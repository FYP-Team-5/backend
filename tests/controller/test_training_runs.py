import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import select

from app.db import GradingConflictError
from app.dto.training import TrainingStartResponse
from app.service.demo_data import DEMO_TRAINING_DATASET
from app.service.model_registry import control, runs
from tests.controller.test_submission_workflow import student
from tests.controller.test_training_dataset_api import upload_csv, wait_version
from tests.service.test_training_service import ARN


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


def eligible(payload):
    """Metrics that pass the automatic promotion gate (see auto_deploy_eligible)."""
    payload["metrics"].update(final_score_qwk=0.9, invalid_output_rate=0.0)
    payload["baseline_metrics"].update(final_score_qwk=0.7, invalid_output_rate=0.01)
    return payload


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
    assert rows[0]["requested_by"] is None
    assert "token" not in rows[0]


def test_auto_deploy_promotes_passing_candidates_without_confirmation(
    client, settings, instructor_headers, monkeypatch
):
    settings.training_auto_deploy_enabled = True
    registry = client.app.state.model_registry
    monkeypatch.setattr(
        registry,
        "check_and_load",
        AsyncMock(return_value={"smoke_tests": "passed"}),
    )
    register(client, settings, eligible(candidate()))
    row = wait_status(client, instructor_headers, "a" * 32, "deployed")
    assert row["is_active"] and row["requested_by"] == "auto-deploy"
    assert registry.active_config()["name"] == "candidate-" + "a" * 32
    # Callback replays stay idempotent and never re-queue a deployed run.
    register(client, settings, eligible(candidate()))
    row = client.get("/api/v1/training-runs", headers=instructor_headers).json()[0]
    assert row["status"] == "deployed" and row["is_active"]


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["metrics"].update(final_score_mae=0.5),
        lambda p: p["metrics"].update(final_score_qwk=0.7),
        lambda p: p["metrics"].update(valid_examples=9),
        lambda p: p["metrics"].update(invalid_output_rate=0.05),
        lambda p: p["metrics"].pop("final_score_qwk"),
        lambda p: p["baseline_metrics"].pop("final_score_qwk"),
    ],
)
def test_auto_deploy_leaves_regressions_and_incomplete_metrics_as_candidates(
    client, settings, instructor_headers, change
):
    settings.training_auto_deploy_enabled = True
    payload = eligible(candidate())
    change(payload)
    register(client, settings, payload)
    rows = client.get("/api/v1/training-runs", headers=instructor_headers).json()
    assert rows[0]["status"] == "candidate" and rows[0]["requested_by"] is None
    registry = client.app.state.model_registry
    with registry.engine.connect() as connection:
        assert connection.execute(select(control.c.pending_run_id)).scalar_one() is None


def test_auto_deploy_defers_while_another_deployment_is_pending(
    client, settings, instructor_headers, monkeypatch
):
    settings.training_auto_deploy_enabled = True
    registry = client.app.state.model_registry
    release = asyncio.Event()

    async def hold(manifest):
        await release.wait()
        return {"smoke_tests": "passed"}

    monkeypatch.setattr(registry, "check_and_load", hold)
    register(client, settings, eligible(candidate()))
    wait_status(client, instructor_headers, "a" * 32, "deploying")
    register(client, settings, eligible(candidate("d" * 32)))
    rows = client.get("/api/v1/training-runs", headers=instructor_headers).json()
    assert {row["run_id"]: row["status"] for row in rows} == {
        "a" * 32: "deploying",
        "d" * 32: "candidate",
    }
    release.set()
    wait_status(client, instructor_headers, "a" * 32, "deployed")


def test_training_api_openapi_describes_current_request_and_response_contracts(client):
    schema = client.get("/openapi.json").json()
    routes = schema["paths"]
    callback = routes["/api/v1/training-runs"]["post"]
    request = callback["requestBody"]["content"]["application/json"]["schema"]
    assert "output_contract" in request["required"]
    assert request["properties"]["output_contract"]["enum"] == [
        "criteria_array_v1",
        "grading_json_v1",
    ]
    response = routes["/api/v1/training-runs"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response["items"]["$ref"].endswith("/TrainingRunResponse")
    detail = schema["components"]["schemas"]["TrainingRunResponse"]
    assert "requested_by" in detail["properties"]
    deployment = routes["/api/v1/training-runs/{run_id}/deploy"]["post"]["responses"][
        "202"
    ]["content"]["application/json"]["schema"]
    assert deployment["$ref"].endswith("/TrainingDeploymentResponse")
    manual = routes["/api/v1/training-runs/start"]["post"]
    assert "202" in manual["responses"]
    assert (
        "request_id"
        in schema["components"]["schemas"]["TrainingStartRequest"]["required"]
    )
    in_progress = routes["/api/v1/training-runs/in-progress"]["get"]
    assert in_progress["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["items"]["$ref"].endswith("/TrainingRunInProgress")


def test_in_progress_training_runs_report_workflow_state(client, instructor_headers):
    started = "2026-11-11T00:00:00Z"
    service = MagicMock()
    service.in_progress.return_value = [
        {
            "run_id": "a" * 32,
            "execution_name": "manual-" + "b" * 64,
            "status": "training",
            "started_at": started,
            "stopped_at": None,
        }
    ]
    client.app.state.training_service = service
    assert client.get("/api/v1/training-runs/in-progress").status_code == 401
    student_headers, _ = student(client, "manual-in-progress")
    assert (
        client.get(
            "/api/v1/training-runs/in-progress", headers=student_headers
        ).status_code
        == 403
    )
    response = client.get(
        "/api/v1/training-runs/in-progress", headers=instructor_headers
    )
    assert response.status_code == 200
    assert response.json() == [
        {
            "run_id": "a" * 32,
            "execution_name": "manual-" + "b" * 64,
            "status": "training",
            "started_at": started,
            "stopped_at": None,
        }
    ]


def test_manual_training_is_instructor_only(client, instructor_headers):
    starter = MagicMock()
    client.app.state.training_service = starter
    payload = {"request_id": str(uuid4())}
    assert client.post("/api/v1/training-runs/start", json=payload).status_code == 401
    student_headers, _ = student(client, "manual")
    assert (
        client.post(
            "/api/v1/training-runs/start", json=payload, headers=student_headers
        ).status_code
        == 403
    )
    starter.start.assert_not_called()
    starter.start.return_value = TrainingStartResponse(
        request_id=payload["request_id"], execution_arn="arn:example:execution"
    )
    response = client.post(
        "/api/v1/training-runs/start", json=payload, headers=instructor_headers
    )
    assert response.status_code == 202
    assert response.json()["request_id"] == payload["request_id"]
    assert starter.start.call_args.args[0].startswith("2")


def test_unconfigured_manual_training_returns_actionable_error(
    client, instructor_headers
):
    response = client.post(
        "/api/v1/training-runs/start",
        json={"request_id": str(uuid4())},
        headers=instructor_headers,
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_mock_dataset_upload_can_start_normal_training_workflow(
    client, settings, instructor_headers, monkeypatch
):
    version = upload_csv(client, instructor_headers, DEMO_TRAINING_DATASET.read_bytes())
    ready = wait_version(client, instructor_headers, version["id"])
    assert ready["status"] == "ready" and ready["is_active"]
    assert ready["row_count"] == 10

    settings.training_state_machine_arn = ARN
    session = MagicMock()
    workflow = Mock(spec=["start_execution", "close"])
    session.client.return_value = workflow
    workflow.start_execution.return_value = {"executionArn": ARN + ":mock-training"}
    monkeypatch.setattr("app.service.training_service.boto3.Session", lambda: session)
    response = client.post(
        "/api/v1/training-runs/start",
        headers=instructor_headers,
        json={"request_id": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    workflow.start_execution.assert_called_once()
    workflow.close.assert_called_once_with()
    assert workflow.start_execution.call_args.kwargs["stateMachineArn"] == ARN

    payload = candidate()
    payload["metrics"] = {"valid_examples": 1, "final_score_mae": 0}
    payload["configuration"].update(
        held_out_evaluation=True, split_strategy="question_group_80_10_10"
    )
    payload["dataset_version"] = version["id"]
    payload["dataset_checksum"] = ready["checksum"]
    payload["dataset_stats"] = {"training_rows": 10, "unique_questions": 10}
    register(client, settings, payload)
    saved = client.get("/api/v1/training-runs", headers=instructor_headers).json()[0]
    assert saved["configuration"]["held_out_evaluation"] is True
    assert saved["dataset_stats"]["training_rows"] == 10


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"request_id": "invalid"},
        {"request_id": str(uuid4()), "force_retrain": False},
    ],
)
def test_manual_training_validates_request(client, instructor_headers, payload):
    starter = MagicMock()
    client.app.state.training_service = starter
    assert (
        client.post(
            "/api/v1/training-runs/start", json=payload, headers=instructor_headers
        ).status_code
        == 422
    )
    starter.start.assert_not_called()


@pytest.mark.parametrize(
    "error,status",
    [
        (GradingConflictError("Upload a ready training dataset first."), 409),
        (EndpointConnectionError(endpoint_url="https://private.example"), 503),
        (
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "sensitive-value"}},
                "StartExecution",
            ),
            503,
        ),
    ],
)
def test_manual_training_errors_do_not_expose_sdk_details(
    client, instructor_headers, error, status
):
    starter = MagicMock()
    client.app.state.training_service = starter
    starter.start.side_effect = error
    response = client.post(
        "/api/v1/training-runs/start",
        json={"request_id": str(uuid4())},
        headers=instructor_headers,
    )
    assert response.status_code == status
    assert "sensitive-value" not in response.text
    assert "private.example" not in response.text


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
        ("GET", "/training-runs/in-progress", None),
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
        "feedback_name": settings.llm_feedback_model,
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


def test_public_adapter_download_does_not_use_hf_credentials(
    client, settings, tmp_path, monkeypatch
):
    import json
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    settings.training_adapter_directory = str(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "unused-upload-token")

    def download(**kwargs):
        directory = Path(kwargs["local_dir"])
        directory.mkdir(parents=True)
        (directory / "adapter_config.json").write_text(
            json.dumps({"base_model_name_or_path": "Qwen/Qwen3.5-9B", "r": 48})
        )
        (directory / "adapter_model.safetensors").write_bytes(b"example weights")

    snapshot = MagicMock(side_effect=download)
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot)
    )
    manifest = candidate()
    client.app.state.model_registry.download(manifest)
    assert snapshot.call_args.kwargs["token"] is False
    assert snapshot.call_args.kwargs["revision"] == manifest["hf_revision"]
