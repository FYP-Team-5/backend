import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import pytest
from boto3.session import Session
from botocore.exceptions import ClientError
from botocore.stub import Stubber

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
    # Match the SDK: a client has close(), but no __enter__/__exit__.
    workflow = Mock(spec=["start_execution", "exceptions", "close"])
    workflow.exceptions = SimpleNamespace(ExecutionAlreadyExists=ExecutionAlreadyExists)
    workflow.start_execution.return_value = {"executionArn": ARN + ":example"}
    session = MagicMock()
    session.client.return_value = workflow
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
    assert workflow.close.call_count == 3


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
    workflow.close.assert_called_once_with()


def test_real_stepfunctions_client_starts_and_closes_without_network(settings):
    service, _, _, session = setup_service(settings)
    workflow = Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    ).client("stepfunctions")
    session.client.return_value = workflow
    execution_arn = (
        "arn:aws:states:us-east-1:123456789012:execution:test-training:example"
    )
    with (
        Stubber(workflow) as stubber,
        patch.object(workflow, "close", wraps=workflow.close) as close,
        patch("app.service.training_service.boto3.Session", return_value=session),
    ):
        stubber.add_response(
            "start_execution",
            {"executionArn": execution_arn, "startDate": datetime.now(UTC)},
        )
        result = service.start("2000001", TrainingStartRequest(request_id=uuid4()))
        assert result.execution_arn == execution_arn
        assert result.status == "accepted"
        stubber.assert_no_pending_responses()
        close.assert_called_once_with()


def test_aws_failure_still_closes_client(settings):
    service, _, workflow, session = setup_service(settings)
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "StartExecution")
    workflow.start_execution.side_effect = error
    with (
        patch("app.service.training_service.boto3.Session", return_value=session),
        pytest.raises(ClientError),
    ):
        service.start("2000001", TrainingStartRequest(request_id=uuid4()))
    workflow.close.assert_called_once_with()


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
