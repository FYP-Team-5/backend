import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.service.model_registry import control, runs
from tests.controller.test_submission_workflow import student


def candidate(run_id="a" * 32):
    return {
        "run_id": run_id,
        "dataset_checksum": "b" * 64,
        "dataset_version": "12345678-1234-1234-1234-123456789abc",
        "completed_at": "2026-11-07T00:00:00+00:00",
        "base_model": "Qwen/Qwen3.5-9B",
        "hf_repo": "SmuFypTeam5/GradingQlora",
        "hf_branch": "candidate-" + run_id,
        "hf_revision": "c" * 40,
        "output_contract": "criteria_array_v1",
        "metrics": {"valid_examples": 20, "final_score_mae": 0.2, "f1": 0.9},
        "baseline_metrics": {"valid_examples": 20, "final_score_mae": 0.4},
        "configuration": {"LORA_R": 48, "LORA_ALPHA": 96},
        "dataset_stats": {"training_rows": 100},
    }


def register(client, settings, payload=None):
    settings.training_callback_token = "machine-token"
    result = client.post(
        "/api/v1/training-runs",
        json=payload or candidate(),
        headers={"X-Training-Token": "machine-token"},
    )
    assert result.status_code == 200, result.text
    return result


def wait_status(client, headers, run_id, status):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        records = client.get("/api/v1/training-runs", headers=headers).json()
        row = next(item for item in records if item["run_id"] == run_id)
        if row["status"] == status:
            return row
        time.sleep(0.01)
    raise AssertionError(records)


def test_callback_requires_separate_machine_credential(
    client, settings, instructor_headers
):
    assert client.post("/api/v1/training-runs", json=candidate()).status_code == 503
    settings.training_callback_token = "machine-token"
    assert (
        client.post(
            "/api/v1/training-runs", json=candidate(), headers=instructor_headers
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/training-runs",
            json=candidate(),
            headers={"X-Training-Token": "wrong"},
        ).status_code
        == 401
    )


def test_registration_replay_and_conflict(client, settings, instructor_headers):
    register(client, settings)
    register(client, settings)
    changed = candidate()
    changed["hf_revision"] = "d" * 40
    assert (
        client.post(
            "/api/v1/training-runs",
            json=changed,
            headers={"X-Training-Token": "machine-token"},
        ).status_code
        == 409
    )
    rows = client.get("/api/v1/training-runs", headers=instructor_headers).json()
    assert len(rows) == 1 and rows[0]["hf_revision"] == "c" * 40
    assert rows[0]["status"] == "candidate" and not rows[0]["is_active"]
    assert "token" not in rows[0]


@pytest.mark.parametrize(
    "change",
    [
        {"hf_repo": "another/repo"},
        {"hf_revision": "main"},
        {"metrics": {"valid_examples": 0, "final_score_mae": 0}},
        {"metrics": {"valid_examples": 1, "final_score_mae": -1}},
        {"output_contract": "unknown"},
        {"completed_at": "2026-11-07T00:00:00"},
    ],
)
def test_invalid_candidate_is_rejected(client, settings, change):
    settings.training_callback_token = "machine-token"
    assert (
        client.post(
            "/api/v1/training-runs",
            json={**candidate(), **change},
            headers={"X-Training-Token": "machine-token"},
        ).status_code
        == 422
    )


def test_review_and_deployment_reject_students_and_callback_tokens(client, settings):
    register(client, settings)
    headers, _ = student(client, 42)
    for method, path, payload in (
        ("GET", "/training-runs", None),
        ("POST", f"/training-runs/{'a' * 32}/deploy", {"revision": 0}),
    ):
        assert (
            client.request(
                method, "/api/v1" + path, headers=headers, json=payload
            ).status_code
            == 403
        )
        assert (
            client.request(
                method,
                "/api/v1" + path,
                headers={"X-Training-Token": "machine-token"},
                json=payload,
            ).status_code
            == 401
        )


def test_explicit_deployment_checks_then_switches_and_failed_candidate_preserves_active(
    client, settings, instructor_headers, monkeypatch
):
    register(client, settings)
    registry = client.app.state.model_registry
    check = AsyncMock(
        return_value={"grading_contract": "passed", "smoke_tests": "passed"}
    )
    monkeypatch.setattr(registry, "check_and_load", check)
    response = client.post(
        f"/api/v1/training-runs/{'a' * 32}/deploy",
        headers=instructor_headers,
        json={"revision": 0},
    )
    assert response.status_code == 202
    row = wait_status(client, instructor_headers, "a" * 32, "deployed")
    assert row["is_active"] and row["checks"]["smoke_tests"] == "passed"
    assert registry.active_config()["name"] == "candidate-" + "a" * 32
    register(client, settings, candidate("d" * 32))
    monkeypatch.setattr(
        registry, "check_and_load", AsyncMock(side_effect=ValueError("invalid output"))
    )
    assert (
        client.post(
            f"/api/v1/training-runs/{'d' * 32}/deploy",
            headers=instructor_headers,
            json={"revision": 0},
        ).status_code
        == 202
    )
    failed = wait_status(client, instructor_headers, "d" * 32, "failed")
    assert not failed["is_active"] and failed["deployment_error"]
    assert registry.active_config()["name"] == "candidate-" + "a" * 32


def test_stale_deploy_is_rejected_and_stale_worker_cannot_activate(
    client, settings, instructor_headers
):
    register(client, settings)
    assert (
        client.post(
            f"/api/v1/training-runs/{'a' * 32}/deploy",
            headers=instructor_headers,
            json={"revision": 4},
        ).status_code
        == 409
    )
    registry = client.app.state.model_registry
    registry.finish("a" * 32, "old-token", {"checks": "passed"})
    assert registry.active_config() is None
    with registry.engine.connect() as connection:
        assert connection.execute(select(runs.c.status)).scalar_one() == "candidate"
        assert connection.execute(select(control.c.active_run_id)).scalar_one() is None


def test_binary_client_contract_is_strict_and_uses_training_prompt():
    import httpx

    from app.service.llm_client import LLMResponseError, LocalLLMClient

    requests = []

    def respond(request):
        import json

        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "[1, 0]" if len(requests) == 1 else "[true, 0]"
                        }
                    }
                ]
            },
        )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            llm = LocalLLMClient(
                url="http://model/v1/chat/completions",
                model="candidate",
                client=transport,
            )
            arguments = {
                "question": "question",
                "desired_answer": "answer",
                "student_answer": "student",
                "criteria": ["one", "two"],
            }
            assert await llm.grade_binary(**arguments) == [1, 0]
            with pytest.raises(LLMResponseError):
                await llm.grade_binary(**arguments)

    asyncio.run(check())
    assert "response_format" not in requests[0]
    assert requests[0]["model"] == "candidate"
    assert "1. one\n2. two" in requests[0]["messages"][1]["content"]


def test_deployed_binary_adapter_uses_weights_and_freezes_model_for_accepted_jobs(
    client, settings, instructor_headers, llm_client, monkeypatch
):
    from app.db.grading_repository import grading_jobs
    from tests.controller.test_submission_workflow import (
        make_test,
        submit,
        wait_for_result,
    )

    registry = client.app.state.model_registry
    monkeypatch.setattr(
        registry, "check_and_load", AsyncMock(return_value={"smoke_tests": "passed"})
    )
    monkeypatch.setattr(registry, "ensure_model", AsyncMock())
    register(client, settings)
    client.post(
        f"/api/v1/training-runs/{'a' * 32}/deploy",
        headers=instructor_headers,
        json={"revision": 0},
    )
    wait_status(client, instructor_headers, "a" * 32, "deployed")
    llm_client.grade_binary.return_value = [1]
    quiz = make_test(client, instructor_headers)
    headers, _ = student(client, 77)
    attempt_id = submit(client, quiz, headers)
    result = wait_for_result(client, quiz["id"], attempt_id, headers)
    assert result["attempt"]["status"] == "graded"
    assert result["total_score"] == 4
    llm_client.grade_binary.assert_called_once()
    assert llm_client.grade_binary.call_args.kwargs["model"] == "candidate-" + "a" * 32
    with registry.engine.connect() as connection:
        saved = connection.execute(
            select(grading_jobs.c.snapshot).where(
                grading_jobs.c.attempt_id == attempt_id
            )
        ).scalar_one()
    assert saved["_grading_model"] == {
        "name": "candidate-" + "a" * 32,
        "contract": "criteria_array_v1",
        "hf_revision": "c" * 40,
        "fewshot_name": settings.llm_model,
    }


def test_actual_deployment_checks_loaded_model_with_both_binary_classes(
    client, monkeypatch
):
    import json

    import httpx

    from app.service import model_registry as module

    registry = client.app.state.model_registry
    monkeypatch.setattr(registry, "download", lambda manifest: "/shared/adapter")
    calls = []

    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/load_lora_adapter"):
            return httpx.Response(200, json={})
        label = (
            "[1, 1]" if "HTTPS encrypts" in body["messages"][1]["content"] else "[0, 0]"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": label}}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    result = asyncio.run(registry.check_and_load(candidate()))
    assert result["smoke_tests"] == "passed"
    assert calls[0][1] == {
        "lora_name": "candidate-" + "a" * 32,
        "lora_path": "/shared/adapter",
    }
    assert len(calls) == 3
    assert registry.active_config() is None  # Checks alone do not promote a model.
