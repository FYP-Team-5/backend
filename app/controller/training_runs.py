"""Authenticated training callbacks and instructor-only model review/deployment."""

import asyncio
import secrets
from typing import Annotated

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError

from app.controller.dependencies import require_api_key, require_instructor
from app.db import GradingConflictError, GradingRecordNotFoundError
from app.dto import UserResponse
from app.dto.training import (
    Candidate,
    DeployRequest,
    TrainingDeploymentResponse,
    TrainingRegistrationResponse,
    TrainingRunInProgress,
    TrainingRunResponse,
    TrainingStartRequest,
    TrainingStartResponse,
)
from app.service.training_service import TrainingUnavailableError

router = APIRouter(tags=["trained models"])


async def require_training_callback(
    request: Request, x_training_token: Annotated[str | None, Header()] = None
):
    token = request.app.state.settings.training_callback_token
    if not token:
        raise HTTPException(503, "Training callbacks are not configured")
    if not x_training_token or not secrets.compare_digest(x_training_token, token):
        raise HTTPException(401, "Invalid training callback credential")


@router.post(
    "/training-runs",
    dependencies=[Depends(require_training_callback)],
    response_model=TrainingRegistrationResponse,
    # Retain the bounded streaming parser while publishing its actual body schema.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": Candidate.model_json_schema(
                        ref_template="#/components/schemas/{model}"
                    )
                }
            },
        }
    },
)
async def register(request: Request):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 1024 * 1024:
            raise HTTPException(413, "Training metadata exceeds 1 MiB")
    try:
        candidate = Candidate.model_validate_json(payload)
        return await asyncio.to_thread(
            request.app.state.model_registry.register, candidate
        )
    except ValidationError:
        raise HTTPException(
            422, "Invalid candidate manifest or evaluation metrics"
        ) from None
    except GradingConflictError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get(
    "/training-runs",
    dependencies=[Depends(require_api_key), Depends(require_instructor)],
    response_model=list[TrainingRunResponse],
)
async def list_runs(request: Request):
    return await asyncio.to_thread(request.app.state.model_registry.list_runs)


@router.get(
    "/training-runs/in-progress",
    dependencies=[Depends(require_api_key), Depends(require_instructor)],
    response_model=list[TrainingRunInProgress],
)
async def list_runs_in_progress(request: Request):
    return await asyncio.to_thread(request.app.state.training_service.in_progress)


@router.post(
    "/training-runs/start",
    status_code=202,
    dependencies=[Depends(require_api_key)],
    response_model=TrainingStartResponse,
)
async def start_training(
    payload: TrainingStartRequest,
    request: Request,
    instructor: Annotated[UserResponse, Depends(require_instructor)],
):
    try:
        return await asyncio.to_thread(
            request.app.state.training_service.start, instructor.id, payload
        )
    except TrainingUnavailableError as error:
        raise HTTPException(503, str(error)) from error
    except GradingConflictError as error:
        raise HTTPException(409, str(error)) from error
    except (ClientError, BotoCoreError):
        raise HTTPException(
            503, "Training could not be confirmed. Retry the same request."
        ) from None


@router.post(
    "/training-runs/{run_id}/deploy",
    status_code=202,
    dependencies=[Depends(require_api_key)],
    response_model=TrainingDeploymentResponse,
)
async def deploy(
    run_id: str,
    payload: DeployRequest,
    request: Request,
    instructor: Annotated[UserResponse, Depends(require_instructor)],
):
    try:
        result = await asyncio.to_thread(
            request.app.state.model_registry.request_deploy,
            run_id,
            payload.revision,
            instructor.id,
        )
        request.app.state.model_registry.wake.set()
        return result
    except GradingRecordNotFoundError:
        raise HTTPException(404, "Candidate not found") from None
    except GradingConflictError as error:
        raise HTTPException(409, str(error)) from error
