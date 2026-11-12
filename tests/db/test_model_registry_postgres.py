"""Model deployment locking and schema tests against a disposable PostgreSQL server.

Every test owns a randomly named schema; existing application tables are untouched.
"""

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, select

from app.config import Settings
from app.db import GradingConflictError
from app.dto.training import Candidate
from app.service.model_registry import ModelRegistry, control, deployments, runs
from tests.controller.test_paired_training import paired_candidate
from tests.controller.test_training_runs import candidate
from tests.db.test_grading_postgres import race

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture
def engine():
    url = os.environ["TEST_DATABASE_URL"]
    schema = "registry_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def registry_for(engine):
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        staff_registration_key="test-staff-registration-key",
        seed_demo_users=False,
    )
    return ModelRegistry(settings, SimpleNamespace(engine=engine))


def test_fresh_database_creates_one_control_row_per_model(engine):
    registry = registry_for(engine)
    registry.prepare()
    registry.prepare()
    with engine.connect() as connection:
        rows = connection.execute(select(control.c.id, control.c.active_run_id)).all()
    assert sorted(rows) == [("feedback", None), ("grading", None)]
    assert registry.active_config() is None


def test_database_with_paired_deployment_tables_upgrades_in_place(engine):
    # Tables as written before per-model deployments: no deployment rows table,
    # one "production" pointer and per-run status columns.
    runs.create(engine)
    control.create(engine)
    paired, grading_only = paired_candidate("a" * 32), candidate("e" * 32)
    with engine.begin() as connection:
        for payload, values in (
            (paired, {"status": "deployed", "revision": 2}),
            (grading_only, {"status": "failed", "revision": 1, "error": "Failed"}),
        ):
            manifest = Candidate.model_validate(payload).model_dump(
                mode="json", exclude_none=True
            )
            connection.execute(
                insert(runs).values(
                    id=payload["run_id"],
                    manifest=manifest,
                    digest="0" * 64,
                    completed_at=payload["completed_at"],
                    **values,
                )
            )
        connection.execute(
            insert(control).values(id="production", active_run_id=paired["run_id"])
        )
    registry = registry_for(engine)
    registry.prepare()
    registry.prepare()
    deployed = {
        run["run_id"]: run["deployments"] for run in registry.list_runs()
    }
    assert deployed[paired["run_id"]]["grading"]["is_active"]
    assert deployed[paired["run_id"]]["feedback"]["is_active"]
    assert deployed[paired["run_id"]]["feedback"]["revision"] == 2
    assert deployed[grading_only["run_id"]]["grading"]["status"] == "failed"
    assert deployed[grading_only["run_id"]]["feedback"] is None
    config = registry.active_config()
    assert config["name"] == "candidate-" + paired["run_id"]
    assert config["feedback_name"] == "feedback-candidate-" + paired["run_id"]


@pytest.fixture
def registry(engine):
    registry = registry_for(engine)
    registry.prepare()
    for run_id in ("a" * 32, "d" * 32):
        registry.register(Candidate.model_validate(paired_candidate(run_id)))
    return registry


def test_concurrent_requests_queue_one_deployment_per_model(registry):
    def request(run_id, model):
        def operation():
            try:
                return registry.request_deploy(run_id, model, 0, "instructor")
            except GradingConflictError:
                return "conflict"

        return operation

    # The same model admits one pending deployment.
    results = race(request("a" * 32, "grading"), request("d" * 32, "grading"))
    assert sorted(map(str, results)) == ["conflict", "{'status': 'queued'}"]
    # A different model has its own queue, even while grading is pending.
    feedback = race(request("a" * 32, "feedback"), request("a" * 32, "feedback"))
    assert sorted(map(str, feedback)) == ["conflict", "{'status': 'queued'}"]
    with registry.engine.connect() as connection:
        pending = dict(
            connection.execute(select(control.c.id, control.c.pending_run_id)).all()
        )
    assert pending["feedback"] == "a" * 32
    assert pending["grading"] in ("a" * 32, "d" * 32)


def test_concurrent_workers_claim_each_pending_model_once(registry):
    registry.request_deploy("a" * 32, "grading", 0, "instructor")
    registry.request_deploy("d" * 32, "feedback", 0, "instructor")
    first, second = race(registry.claim, registry.claim)
    claims = sorted((claim[0], claim[1]["run_id"]) for claim in (first, second))
    assert claims == [("feedback", "d" * 32), ("grading", "a" * 32)]
    assert registry.claim() is None  # Both leases are live.
    # A stale worker cannot finish another model's lease.
    model, manifest, token = first
    other = "feedback" if model == "grading" else "grading"
    registry.finish(other, manifest["run_id"], token, {})
    with registry.engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(deployments.c.model, deployments.c.status).where(
                    deployments.c.status != "candidate"
                )
            ).all()
        )
        active = set(connection.execute(select(control.c.active_run_id)).scalars())
    assert statuses == {"grading": "deploying", "feedback": "deploying"}
    assert active == {None}
    registry.finish(model, manifest["run_id"], token, {"smoke_tests": "passed"})
    config = registry.active_config()
    if model == "grading":
        assert config["name"] == "candidate-" + manifest["run_id"]
    else:
        assert config["feedback_name"] == "feedback-candidate-" + manifest["run_id"]
