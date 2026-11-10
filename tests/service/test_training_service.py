import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.db import GradingConflictError, GradingRecordNotFoundError
from app.dto.training import TrainingStartRequest
from app.service.training_service import TrainingService, TrainingUnavailableError

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test-training"


class ExecutionAlreadyExists(Exception):
    pass


def setup_service(settings):
    settings.training_state_machine_arn = ARN
    repository = MagicMock()
    repository.get_version.return_value = ({"status": "ready"}, {})
    workflow = MagicMock()
    workflow.exceptions = SimpleNamespace(ExecutionAlreadyExists=ExecutionAlreadyExists)
    workflow.start_execution.return_value = {"executionArn": ARN + ":example"}
    session = MagicMock()
    session.client.return_value.__enter__.return_value = workflow
    return TrainingService(settings, repository), repository, workflow, session


def test_manual_training_uses_latest_ready_dataset_and_stable_request(settings):
    service, repository, workflow, session = setup_service(settings)
    request = TrainingStartRequest(request_id=uuid4())
    with patch("app.service.training_service.boto3.Session", return_value=session):
        result = service.start("2000001", request)
        first = workflow.start_execution.call_args.kwargs
        service.start("2000001", request)
        assert workflow.start_execution.call_args.kwargs == first
        service.start("2000002", request)
        assert workflow.start_execution.call_args.kwargs["name"] != first["name"]
    repository.get_version.assert_called_with()
    assert first["stateMachineArn"] == ARN
    assert len(first["name"]) <= 80
    assert json.loads(first["input"]) == {
        "force_retrain": True,
        "requested_by": "2000001",
    }
    assert session.client.call_args.kwargs["region_name"] == "us-east-1"
    assert result.request_id == request.request_id
    assert result.status == "accepted"


def test_retry_after_execution_finishes_returns_original_execution(settings):
    service, _, workflow, session = setup_service(settings)
    workflow.start_execution.side_effect = ExecutionAlreadyExists()
    request = TrainingStartRequest(request_id=uuid4())
    with patch("app.service.training_service.boto3.Session", return_value=session):
        result = service.start("2000001", request)
    name = workflow.start_execution.call_args.kwargs["name"]
    assert (
        result.execution_arn
        == ARN.replace(":stateMachine:", ":execution:") + ":" + name
    )


@pytest.mark.parametrize("missing", [True, False])
def test_unready_dataset_does_not_call_aws(settings, missing):
    service, repository, _, _ = setup_service(settings)
    if missing:
        repository.get_version.side_effect = GradingRecordNotFoundError("latest")
    else:
        repository.get_version.return_value = ({"status": "pending"}, {})
    with patch("app.service.training_service.boto3.Session") as session:
        with pytest.raises(GradingConflictError):
            service.start("2000001", TrainingStartRequest(request_id=uuid4()))
        session.assert_not_called()


def test_unconfigured_manual_training_does_not_call_aws(settings):
    service, _, _, _ = setup_service(settings)
    settings.training_state_machine_arn = None
    with patch("app.service.training_service.boto3.Session") as session:
        with pytest.raises(TrainingUnavailableError):
            service.start("2000001", TrainingStartRequest(request_id=uuid4()))
        session.assert_not_called()
