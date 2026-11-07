"""Durable candidate review and explicitly requested, checked LoRA deployment."""

import asyncio
import hashlib
import json
import math
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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
from app.model.assessment import utc_datetime
from app.service.llm_client import LocalLLMClient


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    dataset_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_version: str = Field(pattern=r"^[a-f0-9-]{36}$")
    completed_at: datetime
    base_model: Literal["Qwen/Qwen3.5-9B"]
    hf_repo: str = Field(max_length=160)
    hf_branch: str = Field(pattern=r"^candidate-[a-f0-9]{32}$")
    hf_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    output_contract: Literal["criteria_array_v1", "grading_json_v1"]
    metrics: dict
    baseline_metrics: dict
    configuration: dict
    dataset_stats: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def branch_matches_run(self):
        if self.hf_branch != "candidate-" + self.run_id:
            raise ValueError("Candidate branch must match its run id")
        return self

    @field_validator("metrics", "baseline_metrics", "configuration", "dataset_stats")
    @classmethod
    def finite_json(cls, value):
        json.dumps(value, allow_nan=False)
        return value

    @field_validator("metrics")
    @classmethod
    def evaluated(cls, value):
        valid = value.get("valid_examples")
        mae = value.get("final_score_mae")
        if type(valid) not in (int, float) or not math.isfinite(valid) or valid < 1:
            raise ValueError("Evaluation must contain valid held-out examples")
        if type(mae) not in (int, float) or not math.isfinite(mae) or mae < 0:
            raise ValueError("Evaluation must contain a finite nonnegative MAE")
        return value

    @field_validator("completed_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("completed_at requires a timezone")
        return value.astimezone(UTC)


runs = Table(
    "training_model_runs",
    grading_metadata,
    Column("id", String(32), primary_key=True),
    Column("manifest", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False, default="candidate"),
    Column("revision", Integer, nullable=False, default=0),
    Column("requested_by", String(128)),
    Column("error", String(1000)),
    Column("checks", JSON),
)
control = Table(
    "training_model_control",
    grading_metadata,
    Column("id", String(20), primary_key=True),
    Column("active_run_id", String(32)),
    Column("pending_run_id", String(32)),
    Column("lease_token", String(36)),
    Column("lease_until", DateTime(timezone=True)),
)


class ModelRegistry:
    def __init__(self, settings, store):
        self.settings, self.engine = settings, store.engine
        self.task = None
        self.wake = asyncio.Event()

    async def initialize(self):
        self.wake = asyncio.Event()

        def initialize():
            grading_metadata.create_all(self.engine)
            # Existing repositories share the same PostgreSQL database.
            from sqlalchemy.exc import IntegrityError

            try:
                with self.engine.begin() as connection:
                    connection.execute(insert(control).values(id="production"))
            except IntegrityError:
                pass

        await asyncio.to_thread(initialize)
        self.task = asyncio.create_task(self.worker(), name="model-deployment-worker")

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def active_config(self):
        with self.engine.connect() as connection:
            active = connection.execute(
                select(runs.c.manifest)
                .join(
                    control,
                    control.c.active_run_id == runs.c.id,
                )
                .where(control.c.id == "production")
            ).scalar_one_or_none()
        if active is None:
            return None
        return {
            "name": "candidate-" + active["run_id"],
            "contract": active["output_contract"],
            "hf_revision": active["hf_revision"],
            "fewshot_name": self.settings.llm_model,
        }

    def describe(self, row, active):
        return {
            **row["manifest"],
            "status": row["status"],
            "revision": row["revision"],
            "is_active": row["id"] == active,
            "deployment_error": row["error"],
            "checks": row["checks"],
            "requested_by": row["requested_by"],
        }

    def list_runs(self):
        # One statement keeps active status and candidate state in the same snapshot.
        with self.engine.connect() as connection:
            query = (
                select(runs, control.c.active_run_id)
                .select_from(runs.outerjoin(control, control.c.id == "production"))
                .order_by(runs.c.completed_at.desc(), runs.c.id.desc())
                .limit(100)
            )
            return [
                self.describe(row, row["active_run_id"])
                for row in connection.execute(query).mappings()
            ]

    def register(self, candidate):
        if candidate.hf_repo != self.settings.training_hf_repo:
            raise ValueError("Candidate repository is not allowed")
        manifest = candidate.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        from sqlalchemy.exc import IntegrityError

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
        except IntegrityError:
            with self.engine.connect() as connection:
                original = connection.execute(
                    select(runs.c.digest).where(runs.c.id == candidate.run_id)
                ).scalar_one()
            if original != digest:
                raise GradingConflictError(
                    "This run id was already registered with different metadata"
                ) from None
        return {"run_id": candidate.run_id, "registered": True}

    def request_deploy(self, run_id, revision, instructor_id):
        with self.engine.begin() as connection:
            owner = (
                connection.execute(
                    select(control)
                    .where(control.c.id == "production")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            row = (
                connection.execute(
                    select(runs).where(runs.c.id == run_id).with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise GradingRecordNotFoundError(run_id)
            if owner["active_run_id"] == run_id:
                return {"status": "deployed"}
            if row["revision"] != revision or owner["pending_run_id"]:
                raise GradingConflictError(
                    "Another deployment or newer review is in progress. Refresh and retry."
                )
            connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="queued",
                    requested_by=instructor_id,
                    error=None,
                    revision=row["revision"] + 1,
                )
            )
            connection.execute(
                update(control)
                .where(control.c.id == "production")
                .values(pending_run_id=run_id)
            )
        return {"status": "queued"}

    def claim(self):
        with self.engine.begin() as connection:
            owner = (
                connection.execute(
                    select(control)
                    .where(control.c.id == "production")
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if not owner["pending_run_id"] or (
                owner["lease_until"]
                and utc_datetime(owner["lease_until"]) > datetime.now(UTC)
            ):
                return None
            row = (
                connection.execute(
                    select(runs).where(runs.c.id == owner["pending_run_id"])
                )
                .mappings()
                .one()
            )
            token = str(uuid.uuid4())
            connection.execute(
                update(control)
                .where(control.c.id == "production")
                .values(
                    lease_token=token,
                    lease_until=datetime.now(UTC) + timedelta(minutes=15),
                )
            )
            connection.execute(
                update(runs).where(runs.c.id == row["id"]).values(status="deploying")
            )
            return row["manifest"], token

    def finish(self, run_id, token, checks, error=None):
        with self.engine.begin() as connection:
            owner = (
                connection.execute(
                    select(control)
                    .where(control.c.id == "production")
                    .with_for_update()
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
                update(control).where(control.c.id == "production").values(**values)
            )
            connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="failed" if error else "deployed",
                    error=error,
                    checks=checks,
                    revision=runs.c.revision + 1,
                )
            )

    def download(self, manifest):
        from huggingface_hub import snapshot_download

        directory = Path(self.settings.training_adapter_directory) / (
            "candidate-" + manifest["run_id"]
        )
        snapshot_download(
            repo_id=manifest["hf_repo"],
            revision=manifest["hf_revision"],
            token=self.settings.training_hf_token or False,
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
        # Restore approved, pinned adapters after host cache loss; never change the active pointer.
        name = config.get("name", "")
        if not name.startswith("candidate-"):
            return
        directory = Path(self.settings.training_adapter_directory) / name
        if all(
            (directory / item).is_file()
            for item in ("adapter_config.json", "adapter_model.safetensors")
        ):
            return

        def manifest_for_job():
            with self.engine.connect() as connection:
                manifest = connection.execute(
                    select(runs.c.manifest).where(
                        runs.c.id == name.removeprefix("candidate-")
                    )
                ).scalar_one()
            if manifest["hf_revision"] != config["hf_revision"]:
                raise ValueError(
                    "Saved grading revision no longer matches the candidate"
                )
            return manifest

        manifest = await asyncio.to_thread(manifest_for_job)
        await asyncio.to_thread(self.download, manifest)

    async def check_and_load(self, manifest):
        path = await asyncio.to_thread(self.download, manifest)
        name = "candidate-" + manifest["run_id"]
        root = self.settings.llm_url.rsplit("/chat/completions", 1)[0]
        headers = (
            {"Authorization": f"Bearer {self.settings.llm_api_key}"}
            if self.settings.llm_api_key
            else {}
        )
        checks = {"pinned_hf_revision": manifest["hf_revision"], "adapter": "passed"}
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            models = await client.get(root + "/models")
            models.raise_for_status()
            available = {item["id"] for item in models.json()["data"]}
            if name not in available:
                loaded = await client.post(
                    root + "/load_lora_adapter",
                    json={"lora_name": name, "lora_path": path},
                )
                loaded.raise_for_status()
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
                    system_prompt="Return JSON with score, feedback, criteria_met containing criteria_id check and is_met true.",
                    user_prompt="Award one point for mentioning TLS: HTTPS uses TLS.",
                )
                if (
                    result.score != 1
                    or len(result.criteria_met) != 1
                    or result.criteria_met[0].criteria_id != "check"
                    or not result.criteria_met[0].is_met
                ):
                    raise ValueError(
                        "Candidate failed the JSON grading compatibility smoke test"
                    )
        return {**checks, "grading_contract": "passed", "smoke_tests": "passed"}

    async def process(self):
        claimed = await asyncio.to_thread(self.claim)
        if claimed is None:
            return False
        manifest, token = claimed
        try:
            checks = await asyncio.wait_for(self.check_and_load(manifest), timeout=600)
        except asyncio.CancelledError:
            raise  # Expiring persisted lease allows recovery after restart.
        except Exception as error:  # noqa: BLE001 - persist failed durable work without switching production
            # Do not persist token-bearing URLs or remote response bodies.
            await asyncio.to_thread(
                self.finish,
                manifest["run_id"],
                token,
                {},
                f"Compatibility/deployment failed ({type(error).__name__}). Check server configuration and retry.",
            )
        else:
            await asyncio.to_thread(self.finish, manifest["run_id"], token, checks)
        return True

    async def worker(self):
        import logging

        while True:
            self.wake.clear()
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Model deployment worker failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=5)
