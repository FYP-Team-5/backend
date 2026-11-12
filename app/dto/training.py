"""Training callback and model-review API contracts."""

import json
import math
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.dto.grading import GradingOutputContract


class FeedbackCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hf_repo: str = Field(max_length=160)
    hf_branch: str = Field(pattern=r"^candidate-[a-f0-9]{32}$")
    hf_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    configuration: dict
    dataset_stats: dict = Field(default_factory=dict)

    @field_validator("configuration", "dataset_stats")
    @classmethod
    def finite_json(cls, value):
        json.dumps(value, allow_nan=False)
        return value


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
    output_contract: GradingOutputContract
    metrics: dict
    baseline_metrics: dict
    configuration: dict
    dataset_stats: dict = Field(default_factory=dict)
    feedback: FeedbackCandidate | None = None

    @model_validator(mode="after")
    def branch_matches_run(self):
        if self.hf_branch != "candidate-" + self.run_id:
            raise ValueError("Candidate branch must match its run id")
        if self.feedback and self.feedback.hf_branch != "candidate-" + self.run_id:
            raise ValueError("Feedback candidate branch must match its run id")
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
            raise ValueError("Evaluation must contain valid examples")
        if type(mae) not in (int, float) or not math.isfinite(mae) or mae < 0:
            raise ValueError("Evaluation must contain a finite nonnegative MAE")
        return value

    @field_validator("completed_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("completed_at requires a timezone")
        return value.astimezone(UTC)


TrainedModel = Literal["grading", "feedback"]


class ModelDeployment(BaseModel):
    """Deployment state of one adapter; grading and feedback deploy separately."""

    status: Literal["candidate", "queued", "deploying", "deployed", "failed"]
    revision: int = Field(ge=0)
    is_active: bool
    error: str | None
    checks: dict | None
    requested_by: str | None


class ModelDeployments(BaseModel):
    grading: ModelDeployment
    # None for older grading-only runs, which have no feedback adapter.
    feedback: ModelDeployment | None


class TrainingRunResponse(Candidate):
    deployments: ModelDeployments


class TrainingRegistrationResponse(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    registered: Literal[True]


class TrainingRunInProgress(BaseModel):
    """Live workflow view before (or without) a candidate manifest callback."""

    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    execution_name: str = Field(max_length=120)
    status: Literal["training", "failed"]
    started_at: datetime
    stopped_at: datetime | None = None


class DeployRequest(BaseModel):
    model: TrainedModel
    revision: int = Field(ge=0)


class TrainingDeploymentResponse(BaseModel):
    status: Literal["queued", "deployed"]


class TrainingStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID


class TrainingStartResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    request_id: UUID
    execution_arn: str
