"""Durable candidate review and checked, independently deployed grading and feedback LoRAs."""

import asyncio
import hashlib
import json
import math
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    insert,
    select,
    update,
)

from app.db.grading_repository import (
    GradingConflictError,
    GradingRecordNotFoundError,
    grading_metadata,
)
from app.dto.training import Candidate
from app.model.assessment import utc_datetime
from app.service.llm_client import LocalLLMClient

runs = Table(
    "training_model_runs",
    grading_metadata,
    Column("id", String(32), primary_key=True),
    Column("manifest", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    # Paired deployment state from before per-model deployments. create_all()
    # cannot drop columns, so they keep their insert defaults and are read
    # only once, to seed training_model_deployments on existing databases.
    Column("status", String(20), nullable=False, default="candidate"),
    Column("revision", Integer, nullable=False, default=0),
    Column("requested_by", String(128)),
    Column("error", String(1000)),
    Column("checks", JSON),
)


def auto_deploy_eligible(manifest):
    """Hands-off promotion gate for TRAINING_AUTO_DEPLOY_ENABLED.

    A candidate must beat the base model on the headline held-out metrics
    (strictly lower MAE and strictly higher QWK, never a higher invalid
    output rate) with at least ten valid examples. Missing or non-numeric
    metrics keep the candidate manual so the review button still applies.
    """
    metrics = manifest.get("metrics", {})
    baseline = manifest.get("baseline_metrics", {})

    def number(container, key):
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    valid = number(metrics, "valid_examples")
    if valid is None or valid < 10:
        return False
    for key, better in (
        ("final_score_mae", lambda a, b: a < b),
        ("final_score_qwk", lambda a, b: a > b),
    ):
        candidate_value = number(metrics, key)
        base_value = number(baseline, key)
        if (
            candidate_value is None
            or base_value is None
            or not better(candidate_value, base_value)
        ):
            return False
    invalid = number(metrics, "invalid_output_rate")
    base_invalid = number(baseline, "invalid_output_rate")
    if invalid is not None and base_invalid is not None:
        return invalid <= base_invalid
    return True


MODELS = ("grading", "feedback")


def run_models(manifest):
    """Deployable models of a run; older grading-only runs have no feedback."""
    return [model for model in MODELS if model == "grading" or manifest.get("feedback")]


# One row per model ("grading", "feedback"), each with its own active pointer,
# pending deployment and lease. The legacy "production" row held the paired
# pointer before per-model deployments and is read only while seeding.
control = Table(
    "training_model_control",
    grading_metadata,
    Column("id", String(20), primary_key=True),
    Column("active_run_id", String(32)),
    Column("pending_run_id", String(32)),
    Column("lease_token", String(36)),
    Column("lease_until", DateTime(timezone=True)),
)

deployments = Table(
    "training_model_deployments",
    grading_metadata,
    Column("run_id", String(32), primary_key=True),
    Column("model", String(20), primary_key=True),
    Column("status", String(20), nullable=False, default="candidate"),
    Column("revision", Integer, nullable=False, default=0),
    Column("requested_by", String(128)),
    Column("error", String(1000)),
    Column("checks", JSON),
)


def seed_deployments(connection):
    """Split the legacy paired deployment state into per-model rows."""
    legacy = (
        connection.execute(select(control).where(control.c.id == "production"))
        .mappings()
        .first()
    ) or {}
    rows = connection.execute(select(runs)).mappings().all()
    paired = {row["id"] for row in rows if row["manifest"].get("feedback")}
    for row in rows:
        for model in run_models(row["manifest"]):
            connection.execute(
                insert(deployments).values(
                    run_id=row["id"],
                    model=model,
                    status=row["status"],
                    revision=row["revision"],
                    requested_by=row["requested_by"],
                    error=row["error"],
                    checks=row["checks"],
                )
            )
    # A grading-only active run served the configured feedback model. A legacy
    # lease belonged to the previous process, so pending work is claimed again.
    for model in MODELS:
        active, pending = legacy.get("active_run_id"), legacy.get("pending_run_id")
        if model == "feedback":
            active = active if active in paired else None
            pending = pending if pending in paired else None
        connection.execute(
            insert(control).values(
                id=model, active_run_id=active, pending_run_id=pending
            )
        )


class ModelRegistry:
    def __init__(self, settings, store):
        self.settings, self.engine = settings, store.engine
        self.task = None
        self.wake = asyncio.Event()

    def prepare(self):
        """Create tables and seed per-model deployment rows once per database."""
        grading_metadata.create_all(self.engine)
        # Existing repositories share the same PostgreSQL database.
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as connection:
                seeded = connection.execute(
                    select(control.c.id).where(control.c.id == "grading")
                ).first()
                if seeded is None:
                    seed_deployments(connection)
        except IntegrityError:
            pass  # Another process seeded the same rows first.

    async def initialize(self):
        self.wake = asyncio.Event()
        await asyncio.to_thread(self.prepare)
        self.task = asyncio.create_task(self.worker(), name="model-deployment-worker")

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def active_config(self):
        # One statement reads both active pointers from the same snapshot.
        with self.engine.connect() as connection:
            active = {
                model: manifest
                for model, manifest in connection.execute(
                    select(control.c.id, runs.c.manifest)
                    .join(runs, control.c.active_run_id == runs.c.id)
                    .where(control.c.id.in_(MODELS))
                )
            }
        grading, feedback = active.get("grading"), active.get("feedback")
        if grading is None and feedback is None:
            return None
        if grading is None:
            config = {
                "name": self.settings.llm_model,
                "contract": self.settings.llm_grading_contract,
            }
        else:
            config = {
                "name": "candidate-" + grading["run_id"],
                "contract": grading["output_contract"],
                "hf_revision": grading["hf_revision"],
            }
        config["fewshot_name"] = self.settings.llm_model
        if feedback is None:
            config["feedback_name"] = self.settings.llm_feedback_model
        else:
            config["feedback_name"] = "feedback-candidate-" + feedback["run_id"]
            config["feedback_hf_revision"] = feedback["feedback"]["hf_revision"]
        return config

    def list_runs(self):
        # One statement keeps active pointers and per-model state in the same snapshot.
        recent = (
            select(runs.c.id)
            .order_by(runs.c.completed_at.desc(), runs.c.id.desc())
            .limit(100)
            .subquery()
        )
        query = (
            select(runs.c.id, runs.c.manifest, deployments, control.c.active_run_id)
            .select_from(
                runs.join(recent, recent.c.id == runs.c.id)
                .join(deployments, deployments.c.run_id == runs.c.id)
                .outerjoin(control, control.c.id == deployments.c.model)
            )
            .order_by(
                runs.c.completed_at.desc(), runs.c.id.desc(), deployments.c.model
            )
        )
        result = {}
        with self.engine.connect() as connection:
            for row in connection.execute(query).mappings():
                run = result.setdefault(
                    row["id"],
                    {
                        **row["manifest"],
                        "deployments": {"grading": None, "feedback": None},
                    },
                )
                run["deployments"][row["model"]] = {
                    "status": row["status"],
                    "revision": row["revision"],
                    "is_active": row["active_run_id"] == row["id"],
                    "error": row["error"],
                    "checks": row["checks"],
                    "requested_by": row["requested_by"],
                }
        return list(result.values())

    def register(self, candidate: Candidate):
        if candidate.hf_repo != self.settings.training_hf_repo:
            raise ValueError("Candidate repository is not allowed")
        if candidate.feedback and (
            candidate.feedback.hf_repo != self.settings.training_feedback_hf_repo
            or candidate.feedback.hf_repo == candidate.hf_repo
        ):
            raise ValueError("Feedback candidate repository is not allowed")
        # Keep historical grading-only callback digests stable.
        manifest = candidate.model_dump(mode="json", exclude_none=True)
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        from sqlalchemy.exc import IntegrityError

        inserted = False
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(runs).values(
                        id=candidate.run_id,
                        manifest=manifest,
                        digest=digest,
                        completed_at=candidate.completed_at,
                        status="candidate",
                        revision=0,
                    )
                )
                connection.execute(
                    insert(deployments),
                    [
                        {"run_id": candidate.run_id, "model": model}
                        for model in run_models(manifest)
                    ],
                )
            inserted = True
        except IntegrityError:
            with self.engine.connect() as connection:
                original = connection.execute(
                    select(runs.c.digest).where(runs.c.id == candidate.run_id)
                ).scalar_one()
            if original != digest:
                raise GradingConflictError(
                    "This run id was already registered with different metadata"
                ) from None
        if not inserted:
            # Callback replay of an earlier registration never re-queues.
            return {"run_id": candidate.run_id, "registered": True}
        queued = False
        if self.settings.training_auto_deploy_enabled and auto_deploy_eligible(
            manifest
        ):
            for model in run_models(manifest):
                try:
                    # Same checked pipeline as the buttons: each model switches
                    # only after its own smoke tests pass. A pending deployment
                    # of that model keeps this one a reviewable candidate.
                    self.request_deploy(candidate.run_id, model, 0, "auto-deploy")
                    queued = True
                except GradingConflictError:
                    pass
        return {
            "run_id": candidate.run_id,
            "registered": True,
            "auto_deploy_queued": queued,
        }

    def request_deploy(self, run_id, model, revision, instructor_id):
        with self.engine.begin() as connection:
            owner = (
                connection.execute(
                    select(control).where(control.c.id == model).with_for_update()
                )
                .mappings()
                .one()
            )
            row = (
                connection.execute(
                    select(deployments)
                    .where(
                        deployments.c.run_id == run_id, deployments.c.model == model
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                exists = connection.execute(
                    select(runs.c.id).where(runs.c.id == run_id)
                ).first()
                if exists:
                    raise ValueError("This run has no feedback model to deploy")
                raise GradingRecordNotFoundError(run_id)
            if owner["active_run_id"] == run_id:
                return {"status": "deployed"}
            if row["revision"] != revision or owner["pending_run_id"]:
                raise GradingConflictError(
                    f"Another {model} deployment or newer review is in progress. Refresh and retry."
                )
            connection.execute(
                update(deployments)
                .where(deployments.c.run_id == run_id, deployments.c.model == model)
                .values(
                    status="queued",
                    requested_by=instructor_id,
                    error=None,
                    revision=row["revision"] + 1,
                )
            )
            connection.execute(
                update(control)
                .where(control.c.id == model)
                .values(pending_run_id=run_id)
            )
        return {"status": "queued"}

    def claim(self):
        for model in MODELS:
            with self.engine.begin() as connection:
                owner = (
                    connection.execute(
                        select(control).where(control.c.id == model).with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if not owner["pending_run_id"] or (
                    owner["lease_until"]
                    and utc_datetime(owner["lease_until"]) > datetime.now(UTC)
                ):
                    continue
                run_id = owner["pending_run_id"]
                manifest = connection.execute(
                    select(runs.c.manifest).where(runs.c.id == run_id)
                ).scalar_one()
                token = str(uuid.uuid4())
                connection.execute(
                    update(control)
                    .where(control.c.id == model)
                    .values(
                        lease_token=token,
                        lease_until=datetime.now(UTC) + timedelta(minutes=15),
                    )
                )
                connection.execute(
                    update(deployments)
                    .where(deployments.c.run_id == run_id, deployments.c.model == model)
                    .values(status="deploying")
                )
                return model, manifest, token
        return None

    def finish(self, model, run_id, token, checks, error=None):
        with self.engine.begin() as connection:
            owner = (
                connection.execute(
                    select(control).where(control.c.id == model).with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                owner["lease_token"] != token
                or owner["pending_run_id"] != run_id
                or owner["lease_until"] is None
                or utc_datetime(owner["lease_until"]) <= datetime.now(UTC)
            ):
                return
            values = {"pending_run_id": None, "lease_token": None, "lease_until": None}
            if error is None:
                values["active_run_id"] = run_id
            connection.execute(
                update(control).where(control.c.id == model).values(**values)
            )
            connection.execute(
                update(deployments)
                .where(deployments.c.run_id == run_id, deployments.c.model == model)
                .values(
                    status="failed" if error else "deployed",
                    error=error,
                    checks=checks,
                    revision=deployments.c.revision + 1,
                )
            )

    def download(self, manifest, *, feedback=False):
        from huggingface_hub import snapshot_download

        directory = Path(self.settings.training_adapter_directory) / (
            ("feedback-candidate-" if feedback else "candidate-") + manifest["run_id"]
        )
        adapter = manifest["feedback"] if feedback else manifest
        snapshot_download(
            repo_id=adapter["hf_repo"],
            revision=adapter["hf_revision"],
            token=False,
            local_dir=directory,
            allow_patterns=["adapter_config.json", "adapter_model.safetensors"],
        )
        config = json.loads((directory / "adapter_config.json").read_text())
        if config.get("base_model_name_or_path") != manifest["base_model"]:
            raise ValueError("Adapter base model is incompatible")
        if (
            type(config.get("r")) is not int
            or not 1 <= config["r"] <= self.settings.training_max_lora_rank
        ):
            raise ValueError("Adapter rank exceeds serving capacity")
        if not (directory / "adapter_model.safetensors").is_file():
            raise ValueError("Adapter weights are missing")
        return str(directory)

    async def ensure_model(self, config):
        # Restore approved, pinned adapters after host cache loss; never change the
        # active pointers. Grading and feedback may come from different runs.
        pinned = []
        name = config.get("name", "")
        if name.startswith("candidate-"):
            pinned.append((name, False, config.get("hf_revision")))
        feedback_name = config.get("feedback_name", "")
        if feedback_name.startswith("feedback-candidate-"):
            pinned.append((feedback_name, True, config.get("feedback_hf_revision")))
        directory = Path(self.settings.training_adapter_directory)
        for adapter_name, is_feedback, revision in pinned:
            if all(
                (directory / adapter_name / item).is_file()
                for item in ("adapter_config.json", "adapter_model.safetensors")
            ):
                continue
            run_id = adapter_name.rsplit("candidate-", 1)[1]

            def manifest_for_job(run_id=run_id):
                with self.engine.connect() as connection:
                    return connection.execute(
                        select(runs.c.manifest).where(runs.c.id == run_id)
                    ).scalar_one()

            manifest = await asyncio.to_thread(manifest_for_job)
            saved = manifest.get("feedback", {}) if is_feedback else manifest
            if saved.get("hf_revision") != revision:
                kind = "feedback" if is_feedback else "grading"
                raise ValueError(
                    f"Saved {kind} revision no longer matches the candidate"
                )
            if is_feedback:
                await asyncio.to_thread(self.download, manifest, feedback=True)
            else:
                await asyncio.to_thread(self.download, manifest)

    def serving_client(self):
        headers = (
            {"Authorization": f"Bearer {self.settings.llm_api_key}"}
            if self.settings.llm_api_key
            else {}
        )
        return httpx.AsyncClient(headers=headers, timeout=120)

    async def load_adapter(self, client, name, path):
        root = self.settings.llm_url.rsplit("/chat/completions", 1)[0]
        models = await client.get(root + "/models")
        models.raise_for_status()
        if name not in {item["id"] for item in models.json()["data"]}:
            loaded = await client.post(
                root + "/load_lora_adapter",
                json={"lora_name": name, "lora_path": path},
            )
            loaded.raise_for_status()

    async def check_and_load(self, model, manifest):
        if model == "feedback":
            return await self.check_feedback(manifest)
        return await self.check_grading(manifest)

    async def check_grading(self, manifest):
        path = await asyncio.to_thread(self.download, manifest)
        name = "candidate-" + manifest["run_id"]
        async with self.serving_client() as client:
            await self.load_adapter(client, name, path)
            llm = LocalLLMClient(url=self.settings.llm_url, model=name, client=client)
            if manifest["output_contract"] == "criteria_array_v1":
                # Test both classes and response length with two independent inputs.
                for answer, expected in (
                    ("HTTPS encrypts traffic using TLS.", [1, 1]),
                    ("I do not know.", [0, 0]),
                ):
                    actual = await llm.grade_binary(
                        question="Why is HTTPS secure?",
                        desired_answer="It encrypts traffic using TLS.",
                        student_answer=answer,
                        criteria=["Mentions encryption", "Mentions TLS"],
                    )
                    if actual != expected:
                        raise ValueError(
                            "Candidate failed the rubric compatibility smoke test"
                        )
            else:
                result = await llm.grade(
                    system_prompt='Return JSON with criteria_met containing criteria_id "check" and boolean is_met. Do not generate a score or feedback.',
                    user_prompt="Criterion check: Mentions TLS. Student answer: HTTPS uses TLS.",
                )
                if (
                    len(result.criteria_met) != 1
                    or result.criteria_met[0].criteria_id != "check"
                    or not result.criteria_met[0].is_met
                ):
                    raise ValueError(
                        "Candidate failed the JSON grading compatibility smoke test"
                    )
        return {
            "pinned_hf_revision": manifest["hf_revision"],
            "adapter": "passed",
            "grading_contract": "passed",
            "smoke_tests": "passed",
        }

    async def check_feedback(self, manifest):
        path = await asyncio.to_thread(self.download, manifest, feedback=True)
        name = "feedback-candidate-" + manifest["run_id"]
        async with self.serving_client() as client:
            await self.load_adapter(client, name, path)
            llm = LocalLLMClient(url=self.settings.llm_url, model=name, client=client)
            result = await llm.generate_feedback(
                model=name,
                system_prompt="You are a computer science tutor. Give concise feedback in plain text.",
                user_prompt="Question: Why is HTTPS secure?\nReference answer: HTTPS encrypts traffic using TLS.\nStudent answer: HTTPS uses TLS encryption.\nKey points: 1. [MET] Mentions encryption 2. [MET] Mentions TLS",
            )
            if not result.feedback.strip():
                raise ValueError("Feedback candidate returned empty text")
        return {
            "pinned_hf_revision": manifest["feedback"]["hf_revision"],
            "adapter": "passed",
            "feedback_response": "passed",
        }

    async def process(self):
        claimed = await asyncio.to_thread(self.claim)
        if claimed is None:
            return False
        model, manifest, token = claimed
        try:
            checks = await asyncio.wait_for(
                self.check_and_load(model, manifest), timeout=600
            )
        except asyncio.CancelledError:
            raise  # Expiring persisted lease allows recovery after restart.
        except Exception as error:  # noqa: BLE001 - persist failed durable work without switching production
            # Do not persist token-bearing URLs or remote response bodies.
            await asyncio.to_thread(
                self.finish,
                model,
                manifest["run_id"],
                token,
                {},
                f"{model.capitalize()} compatibility/deployment failed ({type(error).__name__}). Check server configuration and retry.",
            )
        else:
            await asyncio.to_thread(
                self.finish, model, manifest["run_id"], token, checks
            )
        return True

    async def worker(self):
        import logging

        while True:
            self.wake.clear()
            try:
                # Drain queued work: a grading and a feedback deployment can both wait.
                processed = await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Model deployment worker failed")
                processed = False
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
