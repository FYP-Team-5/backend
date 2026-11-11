"""Instructor requests for the existing retraining workflow."""

import hashlib
import json
from contextlib import closing

import boto3
from botocore.config import Config

from app.config import Settings
from app.db import GradingConflictError, GradingRecordNotFoundError
from app.db.dataset_repository import DatasetRepository
from app.dto.training import TrainingStartRequest, TrainingStartResponse


class TrainingUnavailableError(RuntimeError):
    pass


class TrainingService:
    def __init__(self, settings: Settings, repository: DatasetRepository):
        self.settings = settings
        self.repository = repository

    def start(self, instructor_id: str, request: TrainingStartRequest):
        arn = self.settings.training_state_machine_arn
        if not arn:
            raise TrainingUnavailableError("Manual training is not configured.")
        try:
            row, _ = self.repository.get_version()
        except GradingRecordNotFoundError:
            raise GradingConflictError(
                "Upload a ready training dataset first."
            ) from None
        if row["status"] != "ready":
            raise GradingConflictError("The latest training dataset is not ready.")

        name = (
            "manual-"
            + hashlib.sha256(
                f"{instructor_id}:{request.request_id}".encode()
            ).hexdigest()
        )
        payload = json.dumps(
            {"force_retrain": True, "requested_by": instructor_id}, sort_keys=True
        )
        # Stable input and name make retries safe even when the active CSV changes.
        config = Config(
            connect_timeout=3,
            read_timeout=8,
            retries={"mode": "standard", "total_max_attempts": 2},
        )
        with closing(
            boto3.Session().client(
                "stepfunctions", region_name=arn.split(":")[3], config=config
            )
        ) as workflow:
            try:
                result = workflow.start_execution(
                    stateMachineArn=arn, name=name, input=payload
                )
                execution_arn = result["executionArn"]
            except workflow.exceptions.ExecutionAlreadyExists:
                # A completed execution retains its name; a retry still refers to it.
                execution_arn = (
                    arn.replace(":stateMachine:", ":execution:") + ":" + name
                )
        return TrainingStartResponse(
            request_id=request.request_id, execution_arn=execution_arn
        )
