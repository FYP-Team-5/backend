"""Authenticated training callbacks and instructor-only model review/deployment."""

import asyncio
import secrets
from typing import Annotated

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
    TrainingRunResponse,
)

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
            "content": {"application/json": {"schema": Candidate.model_json_schema()}},
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
