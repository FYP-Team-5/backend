import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.db.grading_repository import grading_jobs
from app.service.llm_client import LLMResponseError
from tests.controller.test_submission_workflow import (
    make_test,
    student,
    submit,
    wait_for_result,
)
from tests.controller.test_training_runs import (
    candidate,
    eligible,
    register,
    wait_status,
)


def paired_candidate(run_id="a" * 32):
    payload = candidate(run_id)
    payload["feedback"] = {
        "hf_repo": "SmuFypTeam5/feedback",
        "hf_branch": "candidate-" + run_id,
        "hf_revision": "d" * 40,
        "configuration": {"LORA_R": 16, "LORA_ALPHA": 32, "evaluation_metrics": False},
        "dataset_stats": {"split_rows": {"train": 8, "validation": 1, "test": 1}},
    }
    return payload


def test_paired_callback_is_one_idempotent_run_without_feedback_metrics(
    client, settings, instructor_headers
):
    payload = paired_candidate()
    register(client, settings, payload)
    register(client, settings, payload)
    rows = client.get("/api/v1/training-runs", headers=instructor_headers).json()
    assert len(rows) == 1
    assert rows[0]["feedback"]["hf_revision"] == "d" * 40
    assert "metrics" not in rows[0]["feedback"]
    assert rows[0]["metrics"] == payload["metrics"]


def test_openapi_feedback_reference_resolves_to_response_component(client):
    schema = client.get("/openapi.json").json()
    request = schema["paths"]["/api/v1/training-runs"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    reference = request["properties"]["feedback"]["anyOf"][0]["$ref"]
    assert reference.startswith("#/components/schemas/")
    assert reference.rsplit("/", 1)[-1] in schema["components"]["schemas"]


@pytest.mark.parametrize(
    "change",
    [
        {"hf_repo": "untrusted/feedback"},
        {"hf_branch": "candidate-" + "f" * 32},
        {"hf_revision": "main"},
        {"metrics": {"f1": 1}},
    ],
)
def test_invalid_feedback_candidate_is_rejected(client, settings, change):
    settings.training_callback_token = "machine-token"
    payload = paired_candidate()
    payload["feedback"].update(change)
    response = client.post(
        "/api/v1/training-runs",
        json=payload,
        headers={"X-Training-Token": "machine-token"},
    )
    assert response.status_code == 422


def test_paired_deployment_pins_both_models_for_accepted_jobs(
    client, settings, instructor_headers, llm_client, monkeypatch
):
    registry = client.app.state.model_registry
    monkeypatch.setattr(
        registry,
        "check_and_load",
        AsyncMock(
            return_value={"grading_contract": "passed", "feedback_response": "passed"}
        ),
    )
    monkeypatch.setattr(registry, "ensure_model", AsyncMock())
    register(client, settings, paired_candidate())
    client.post(
        f"/api/v1/training-runs/{'a' * 32}/deploy",
        headers=instructor_headers,
        json={"revision": 0},
    )
    wait_status(client, instructor_headers, "a" * 32, "deployed")
    config = registry.active_config()
    assert config["feedback_name"] == "feedback-candidate-" + "a" * 32
    assert config["feedback_hf_revision"] == "d" * 40
    quiz = make_test(client, instructor_headers)
    headers, _ = student(client, 901)
    attempt_id = submit(client, quiz, headers)
    result = wait_for_result(client, quiz["id"], attempt_id, headers)
    assert result["attempt"]["status"] == "graded"
    assert (
        llm_client.generate_feedback.call_args.kwargs["model"]
        == config["feedback_name"]
    )
    with registry.engine.connect() as connection:
        snapshot = connection.execute(
            select(grading_jobs.c.snapshot).where(
                grading_jobs.c.attempt_id == attempt_id
            )
        ).scalar_one()
    assert snapshot["_grading_model"] == config
    # A subsequent deployment cannot rewrite the models saved in accepted jobs.
    register(client, settings, paired_candidate("e" * 32))
    client.post(
        f"/api/v1/training-runs/{'e' * 32}/deploy",
        headers=instructor_headers,
        json={"revision": 0},
    )
    wait_status(client, instructor_headers, "e" * 32, "deployed")
    with registry.engine.connect() as connection:
        saved = connection.execute(
            select(grading_jobs.c.snapshot).where(
                grading_jobs.c.attempt_id == attempt_id
            )
        ).scalar_one()
    assert saved["_grading_model"] == config


@pytest.fixture
def paired_serving(client, monkeypatch, feedback_text):
    from app.service import model_registry as module

    registry = client.app.state.model_registry
    monkeypatch.setattr(
        registry,
        "download",
        lambda manifest, **kwargs: (
            "/shared/feedback" if kwargs.get("feedback") else "/shared/grading"
        ),
    )
    calls = []

    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/load_lora_adapter"):
            return httpx.Response(200, json={})
        if body["model"].startswith("feedback-"):
            content = feedback_text
        else:
            content = (
                "[1, 1]"
                if "HTTPS encrypts" in body["messages"][1]["content"]
                else "[0, 0]"
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return calls


@pytest.mark.parametrize(
    "feedback_text", ["You correctly explain that TLS encrypts traffic.", ""]
)
def test_actual_paired_checks_load_and_verify_both_adapters(
    client, paired_serving, feedback_text
):
    registry = client.app.state.model_registry
    if not feedback_text:
        with pytest.raises((ValueError, LLMResponseError)):
            asyncio.run(registry.check_and_load(paired_candidate()))
    else:
        checks = asyncio.run(registry.check_and_load(paired_candidate()))
        assert checks["feedback_response"] == "passed"
        assert checks["pinned_feedback_hf_revision"] == "d" * 40
    loaded = [
        body["lora_name"]
        for path, body in paired_serving
        if path.endswith("load_lora_adapter")
    ]
    assert loaded == ["candidate-" + "a" * 32, "feedback-candidate-" + "a" * 32]
    assert registry.active_config() is None


@pytest.mark.parametrize(
    "feedback_text", ["You correctly explain that TLS encrypts traffic.", ""]
)
def test_auto_deploy_checks_pair_before_replacing_active_models(
    client, settings, instructor_headers, monkeypatch, paired_serving, feedback_text
):
    registry = client.app.state.model_registry
    previous_id = "e" * 32
    # Establish the current model; the new candidate uses the real checker below.
    with monkeypatch.context() as initial:
        initial.setattr(registry, "check_and_load", AsyncMock(return_value={}))
        register(client, settings, paired_candidate(previous_id))
        response = client.post(
            f"/api/v1/training-runs/{previous_id}/deploy",
            headers=instructor_headers,
            json={"revision": 0},
        )
        assert response.status_code == 202
        wait_status(client, instructor_headers, previous_id, "deployed")
    previous_config = registry.active_config()

    settings.training_auto_deploy_enabled = True
    payload = eligible(paired_candidate())
    response = register(client, settings, payload)
    assert response.json()["registered"] is True
    status = "deployed" if feedback_text else "failed"
    row = wait_status(client, instructor_headers, "a" * 32, status)
    assert row["requested_by"] == "auto-deploy"
    loaded = [
        body["lora_name"]
        for path, body in paired_serving
        if path.endswith("load_lora_adapter")
    ]
    assert loaded == ["candidate-" + "a" * 32, "feedback-candidate-" + "a" * 32]
    if feedback_text:
        assert row["is_active"]
        assert row["checks"]["grading_contract"] == "passed"
        assert row["checks"]["feedback_response"] == "passed"
        config = registry.active_config()
        assert config["hf_revision"] == payload["hf_revision"]
        assert config["feedback_hf_revision"] == payload["feedback"]["hf_revision"]
    else:
        assert not row["is_active"] and row["deployment_error"]
        assert registry.active_config() == previous_config
    # Replaying the callback cannot retry deployment or make more model calls.
    call_count = len(paired_serving)
    register(client, settings, payload)
    row = wait_status(client, instructor_headers, "a" * 32, status)
    assert len(paired_serving) == call_count


def test_cache_recovery_restores_feedback_even_if_grading_is_cached(
    client, settings, instructor_headers, tmp_path, monkeypatch
):
    settings.training_adapter_directory = str(tmp_path)
    register(client, settings, paired_candidate())
    directory = tmp_path / ("candidate-" + "a" * 32)
    directory.mkdir()
    for name in ("adapter_config.json", "adapter_model.safetensors"):
        (directory / name).write_text("cached")
    # to_thread invokes a regular callable, so record synchronous calls here.
    from unittest.mock import Mock

    download = Mock()
    monkeypatch.setattr(client.app.state.model_registry, "download", download)
    config = {
        "name": "candidate-" + "a" * 32,
        "hf_revision": "c" * 40,
        "feedback_name": "feedback-candidate-" + "a" * 32,
        "feedback_hf_revision": "d" * 40,
    }
    asyncio.run(client.app.state.model_registry.ensure_model(config))
    download.assert_called_once()
    assert download.call_args.kwargs == {"feedback": True}
    config["feedback_hf_revision"] = "e" * 40
    with pytest.raises(ValueError, match="Saved feedback revision"):
        asyncio.run(client.app.state.model_registry.ensure_model(config))


def test_feedback_download_uses_its_own_repository_and_pinned_commit(
    client, settings, tmp_path, monkeypatch
):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock

    settings.training_adapter_directory = str(tmp_path)

    def write_adapter(**kwargs):
        directory = Path(kwargs["local_dir"])
        directory.mkdir()
        (directory / "adapter_config.json").write_text(
            json.dumps({"base_model_name_or_path": "Qwen/Qwen3.5-9B", "r": 16})
        )
        (directory / "adapter_model.safetensors").write_bytes(b"feedback weights")

    snapshot = Mock(side_effect=write_adapter)
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot)
    )
    directory = client.app.state.model_registry.download(
        paired_candidate(), feedback=True
    )
    assert Path(directory).name == "feedback-candidate-" + "a" * 32
    assert snapshot.call_args.kwargs["repo_id"] == "SmuFypTeam5/feedback"
    assert snapshot.call_args.kwargs["revision"] == "d" * 40
    assert snapshot.call_args.kwargs["token"] is False
