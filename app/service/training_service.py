"""Instructor requests for the existing retraining workflow."""

import hashlib
import json
import time
from contextlib import closing

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.db import GradingConflictError, GradingRecordNotFoundError
from app.db.dataset_repository import DatasetRepository
from app.dto.training import TrainingStartRequest, TrainingStartResponse

# A failed run stops showing once the workflow history leaves this window.
FAILURE_LOOKBACK_SECONDS = 36 * 3600


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

    def in_progress(self):
        """Best-effort live view: running executions plus one recent failure.

        Covers nightly and manual runs alike without workflow callbacks; the
        derived run id matches the workflow's so the registered candidate
        later appears under the same identity.
        """
        arn = self.settings.training_state_machine_arn
        if not arn:
            return []
        config = Config(
            connect_timeout=3,
            read_timeout=8,
            retries={"mode": "standard", "total_max_attempts": 1},
        )
        try:
            with closing(
                boto3.Session().client(
                    "stepfunctions", region_name=arn.split(":")[3], config=config
                )
            ) as workflow:
                executions = workflow.list_executions(
                    stateMachineArn=arn, maxResults=50
                )["executions"]
        except (ClientError, BotoCoreError):
            return []
        shown = [e for e in executions if e["status"] == "RUNNING"]
        failure = next(
            (
                e
                for e in executions
                if e["status"] in ("FAILED", "TIMED_OUT", "ABORTED")
                and e.get("stopDate") is not None
                and time.time() - e["stopDate"].timestamp()
                < FAILURE_LOOKBACK_SECONDS
            ),
            None,
        )
        if failure is not None:
            shown.append(failure)
        return [
            {
                "run_id": hashlib.sha256(e["executionArn"].encode()).hexdigest()[
                    :32
                ],
                "execution_name": e["name"],
                "status": "training" if e["status"] == "RUNNING" else "failed",
                "started_at": e["startDate"],
                "stopped_at": e.get("stopDate"),
            }
            for e in sorted(shown, key=lambda e: e["startDate"], reverse=True)
        ]
