import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.db.grading_repository import grading_jobs
from app.service.llm_client import LLMResponseError
from app.service.model_registry import (
    ModelRegistry,
    control,
    deployments,
    runs,
)
from tests.controller.test_submission_workflow import (
    make_test,
    student,
    submit,
    wait_for_result,
)
from tests.controller.test_training_runs import (
    candidate,
    deploy,
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
    assert {
        model: state["status"] for model, state in rows[0]["deployments"].items()
    } == {"grading": "candidate", "feedback": "candidate"}
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
    for model in ("grading", "feedback"):
        deploy(client, instructor_headers, "a" * 32, model)
        wait_status(client, instructor_headers, "a" * 32, "deployed", model)
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
    for model in ("grading", "feedback"):
        deploy(client, instructor_headers, "e" * 32, model)
        wait_status(client, instructor_headers, "e" * 32, "deployed", model)
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
def test_actual_checks_load_and_verify_only_the_requested_adapter(
    client, paired_serving, feedback_text
):
    registry = client.app.state.model_registry
    grading = asyncio.run(registry.check_and_load("grading", paired_candidate()))
    assert grading["smoke_tests"] == "passed"
    assert grading["pinned_hf_revision"] == "c" * 40
    assert "feedback_response" not in grading
    if not feedback_text:
        with pytest.raises((ValueError, LLMResponseError)):
            asyncio.run(registry.check_and_load("feedback", paired_candidate()))
    else:
        checks = asyncio.run(registry.check_and_load("feedback", paired_candidate()))
        assert checks["feedback_response"] == "passed"
        assert checks["pinned_hf_revision"] == "d" * 40
    loaded = [
        body["lora_name"]
        for path, body in paired_serving
        if path.endswith("load_lora_adapter")
    ]
    assert loaded == ["candidate-" + "a" * 32, "feedback-candidate-" + "a" * 32]
    models = [body.get("model") for path, body in paired_serving if "model" in body]
    # Two grading smoke tests, then one feedback response check.
    assert models == ["candidate-" + "a" * 32] * 2 + ["feedback-candidate-" + "a" * 32]
    assert registry.active_config() is None


@pytest.mark.parametrize(
    "feedback_text", ["You correctly explain that TLS encrypts traffic.", ""]
)
def test_auto_deploy_checks_each_model_before_replacing_it(
    client, settings, instructor_headers, monkeypatch, paired_serving, feedback_text
):
    registry = client.app.state.model_registry
    previous_id = "e" * 32
    # Establish the current models; the new candidate uses the real checker below.
    with monkeypatch.context() as initial:
        initial.setattr(registry, "check_and_load", AsyncMock(return_value={}))
        register(client, settings, paired_candidate(previous_id))
        for model in ("grading", "feedback"):
            assert deploy(client, instructor_headers, previous_id, model).status_code == 202
            wait_status(client, instructor_headers, previous_id, "deployed", model)
    previous_config = registry.active_config()

    settings.training_auto_deploy_enabled = True
    payload = eligible(paired_candidate())
    response = register(client, settings, payload)
    assert response.json()["registered"] is True
    feedback_status = "deployed" if feedback_text else "failed"
    grading = wait_status(client, instructor_headers, "a" * 32, "deployed")
    feedback = wait_status(
        client, instructor_headers, "a" * 32, feedback_status, "feedback"
    )
    assert grading["requested_by"] == feedback["requested_by"] == "auto-deploy"
    loaded = [
        body["lora_name"]
        for path, body in paired_serving
        if path.endswith("load_lora_adapter")
    ]
    assert loaded == ["candidate-" + "a" * 32, "feedback-candidate-" + "a" * 32]
    # A failed feedback check no longer holds back a grading model that passed.
    assert grading["is_active"] and grading["checks"]["grading_contract"] == "passed"
    config = registry.active_config()
    assert config["name"] == "candidate-" + "a" * 32
    assert config["hf_revision"] == payload["hf_revision"]
    if feedback_text:
        assert feedback["is_active"]
        assert feedback["checks"]["feedback_response"] == "passed"
        assert config["feedback_hf_revision"] == payload["feedback"]["hf_revision"]
    else:
        assert not feedback["is_active"] and feedback["error"].startswith("Feedback")
        assert config["feedback_name"] == previous_config["feedback_name"]
        assert config["feedback_hf_revision"] == previous_config["feedback_hf_revision"]
    # Replaying the callback cannot retry deployment or make more model calls.
    call_count = len(paired_serving)
    register(client, settings, payload)
    wait_status(client, instructor_headers, "a" * 32, feedback_status, "feedback")
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


def test_grading_and_feedback_from_different_runs_serve_together(
    client, settings, instructor_headers, llm_client, monkeypatch
):
    registry = client.app.state.model_registry
    monkeypatch.setattr(
        registry, "check_and_load", AsyncMock(return_value={"smoke_tests": "passed"})
    )
    monkeypatch.setattr(registry, "ensure_model", AsyncMock())
    older, newer = "e" * 32, "a" * 32
    previous = paired_candidate(older)
    previous["hf_revision"], previous["feedback"]["hf_revision"] = "9" * 40, "f" * 40
    register(client, settings, previous)
    register(client, settings, paired_candidate(newer))
    # Promote the newer grading model while keeping the older feedback model.
    deploy(client, instructor_headers, older, "feedback")
    wait_status(client, instructor_headers, older, "deployed", "feedback")
    deploy(client, instructor_headers, newer, "grading")
    wait_status(client, instructor_headers, newer, "deployed")
    rows = {
        row["run_id"]: row["deployments"]
        for row in client.get("/api/v1/training-runs", headers=instructor_headers).json()
    }
    assert rows[newer]["grading"]["is_active"]
    assert rows[newer]["feedback"]["status"] == "candidate"
    assert not rows[newer]["feedback"]["is_active"]
    assert rows[older]["feedback"]["is_active"]
    assert rows[older]["grading"]["status"] == "candidate"
    config = registry.active_config()
    assert config == {
        "name": "candidate-" + newer,
        "contract": "criteria_array_v1",
        "hf_revision": "c" * 40,
        "fewshot_name": settings.llm_model,
        "feedback_name": "feedback-candidate-" + older,
        "feedback_hf_revision": "f" * 40,
    }
    llm_client.grade_binary.return_value = [1]
    quiz = make_test(client, instructor_headers)
    headers, _ = student(client, 902)
    attempt_id = submit(client, quiz, headers)
    result = wait_for_result(client, quiz["id"], attempt_id, headers)
    assert result["attempt"]["status"] == "graded"
    assert llm_client.grade_binary.call_args.kwargs["model"] == "candidate-" + newer
    assert (
        llm_client.generate_feedback.call_args.kwargs["model"]
        == "feedback-candidate-" + older
    )
    with registry.engine.connect() as connection:
        snapshot = connection.execute(
            select(grading_jobs.c.snapshot).where(
                grading_jobs.c.attempt_id == attempt_id
            )
        ).scalar_one()
    assert snapshot["_grading_model"] == config


def test_feedback_only_deployment_keeps_configured_grading_model(
    client, settings, instructor_headers, monkeypatch
):
    registry = client.app.state.model_registry
    check = AsyncMock(return_value={"feedback_response": "passed"})
    monkeypatch.setattr(registry, "check_and_load", check)
    register(client, settings, paired_candidate())
    assert deploy(client, instructor_headers, "a" * 32, "feedback").status_code == 202
    feedback = wait_status(client, instructor_headers, "a" * 32, "deployed", "feedback")
    assert feedback["is_active"] and feedback["checks"] == {"feedback_response": "passed"}
    assert [call.args[0] for call in check.call_args_list] == ["feedback"]
    rows = client.get("/api/v1/training-runs", headers=instructor_headers).json()
    grading = rows[0]["deployments"]["grading"]
    assert grading["status"] == "candidate" and not grading["is_active"]
    assert registry.active_config() == {
        "name": settings.llm_model,
        "contract": settings.llm_grading_contract,
        "fewshot_name": settings.llm_model,
        "feedback_name": "feedback-candidate-" + "a" * 32,
        "feedback_hf_revision": "d" * 40,
    }


def test_each_model_has_its_own_deployment_queue(
    client, settings, instructor_headers, monkeypatch
):
    registry = client.app.state.model_registry
    release = asyncio.Event()
    checked = []

    async def hold(model, manifest):
        checked.append((model, manifest["run_id"]))
        if model == "grading":
            await release.wait()
        return {"smoke_tests": "passed"}

    monkeypatch.setattr(registry, "check_and_load", hold)
    first, second = "a" * 32, "d" * 32
    register(client, settings, paired_candidate(first))
    register(client, settings, paired_candidate(second))
    assert deploy(client, instructor_headers, first).status_code == 202
    wait_status(client, instructor_headers, first, "deploying")
    # A second grading deployment waits; a feedback deployment does not.
    assert deploy(client, instructor_headers, second).status_code == 409
    response = deploy(client, instructor_headers, second, "feedback")
    assert response.status_code == 202 and response.json() == {"status": "queued"}
    release.set()
    wait_status(client, instructor_headers, first, "deployed")
    wait_status(client, instructor_headers, second, "deployed", "feedback")
    assert checked == [("grading", first), ("feedback", second)]
    # Requesting an already active model is a harmless no-op.
    repeated = deploy(client, instructor_headers, second, "feedback", revision=0)
    assert repeated.status_code == 202 and repeated.json() == {"status": "deployed"}
    assert checked == [("grading", first), ("feedback", second)]


def test_deploy_request_names_an_existing_model(client, settings, instructor_headers):
    register(client, settings)  # Grading-only run.
    missing = deploy(client, instructor_headers, "a" * 32, "feedback")
    assert missing.status_code == 422
    assert missing.json()["detail"] == "This run has no feedback model to deploy"
    assert deploy(client, instructor_headers, "f" * 32, "feedback").status_code == 404
    for payload in ({"revision": 0}, {"model": "both", "revision": 0}):
        response = client.post(
            f"/api/v1/training-runs/{'a' * 32}/deploy",
            headers=instructor_headers,
            json=payload,
        )
        assert response.status_code == 422


def test_cache_recovery_restores_each_adapter_from_its_own_run(
    client, settings, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    settings.training_adapter_directory = str(tmp_path)
    registry = client.app.state.model_registry
    previous = paired_candidate("e" * 32)
    previous["feedback"]["hf_revision"] = "f" * 40
    register(client, settings, previous)
    register(client, settings, paired_candidate())
    download = Mock()
    monkeypatch.setattr(registry, "download", download)
    config = {
        "name": "candidate-" + "a" * 32,
        "hf_revision": "c" * 40,
        "feedback_name": "feedback-candidate-" + "e" * 32,
        "feedback_hf_revision": "f" * 40,
    }
    asyncio.run(registry.ensure_model(config))
    assert [
        (call.args[0]["run_id"], call.kwargs) for call in download.call_args_list
    ] == [("a" * 32, {}), ("e" * 32, {"feedback": True})]
    config["hf_revision"] = "9" * 40
    with pytest.raises(ValueError, match="Saved grading revision"):
        asyncio.run(registry.ensure_model(config))


@pytest.mark.parametrize("active", ["a" * 32, "e" * 32])
def test_existing_paired_deployment_state_is_split_per_model(
    client, settings, instructor_headers, monkeypatch, active
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from sqlalchemy import insert

    registry = client.app.state.model_registry
    # Keep the app's worker idle so seeded state can be inspected as written.
    monkeypatch.setattr(registry, "claim", lambda: None)
    paired, grading_only, pending = "a" * 32, "e" * 32, "d" * 32
    register(client, settings, paired_candidate(paired))
    register(client, settings, candidate(grading_only))
    register(client, settings, paired_candidate(pending))
    # Recreate a database written before per-model deployments.
    with registry.engine.begin() as connection:
        connection.execute(deployments.delete())
        connection.execute(control.delete())
        legacy = {
            active: {
                "status": "deployed",
                "revision": 2,
                "requested_by": "instructor-1",
                "checks": {"smoke_tests": "passed"},
            },
            pending: {"status": "queued", "revision": 1},
        }
        for run_id, values in legacy.items():
            connection.execute(runs.update().where(runs.c.id == run_id).values(**values))
        connection.execute(
            insert(control).values(
                id="production",
                active_run_id=active,
                pending_run_id=pending,
                lease_token="previous-process",
                lease_until=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    upgraded = ModelRegistry(settings, SimpleNamespace(engine=registry.engine))
    for _ in range(2):  # Seeding runs once; later startups keep per-model state.
        upgraded.prepare()

    rows = {
        row["run_id"]: row["deployments"]
        for row in client.get("/api/v1/training-runs", headers=instructor_headers).json()
    }
    assert rows[grading_only]["feedback"] is None
    assert rows[active]["grading"] == {
        "status": "deployed",
        "revision": 2,
        "is_active": True,
        "error": None,
        "checks": {"smoke_tests": "passed"},
        "requested_by": "instructor-1",
    }
    if active == paired:
        assert rows[paired]["feedback"] == rows[paired]["grading"]
    else:
        assert rows[paired]["feedback"]["status"] == "candidate"
        assert not rows[paired]["feedback"]["is_active"]
    assert {model: state["status"] for model, state in rows[pending].items()} == {
        "grading": "queued",
        "feedback": "queued",
    }
    # The models that served before the upgrade keep serving.
    expected = {
        "name": "candidate-" + active,
        "contract": "criteria_array_v1",
        "hf_revision": "c" * 40,
        "fewshot_name": settings.llm_model,
        "feedback_name": "feedback-candidate-" + paired
        if active == paired
        else settings.llm_feedback_model,
    }
    if active == paired:
        expected["feedback_hf_revision"] = "d" * 40
    assert registry.active_config() == expected
    # The previous process's lease is dropped, so the pending pair resumes at once.
    for model in ("grading", "feedback"):
        claimed_model, manifest, _ = upgraded.claim()
        assert (claimed_model, manifest["run_id"]) == (model, pending)
    assert upgraded.claim() is None
