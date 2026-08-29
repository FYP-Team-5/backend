"""Routes under /users."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.controller.dependencies import current_user, get_user_service
from app.db import UserNotFoundError, UserStoreError
from app.dto import (
    UserResponse,
    UserStatusUpdate,
)
from app.service import (
    AuthorizationError,
    UserService,
)

users_router = APIRouter(prefix="/users", tags=["users"])

@users_router.get("/me", response_model=UserResponse)
async def get_me(
    principal: Annotated[UserResponse, Depends(current_user)],
) -> UserResponse:
    return principal


@users_router.get("", response_model=list[UserResponse])
async def list_users(
    principal: Annotated[UserResponse, Depends(current_user)],
    service: Annotated[UserService, Depends(get_user_service)],
    role: Annotated[Literal["student", "staff"] | None, Query()] = None,
) -> list[UserResponse]:
    try:
        return await service.list_users(principal, role=role)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@users_router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    principal: Annotated[UserResponse, Depends(current_user)],
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserResponse:
    try:
        return await service.get_user(principal, user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@users_router.patch(
    "/{user_id}/status",
    response_model=UserResponse,
)
async def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    principal: Annotated[UserResponse, Depends(current_user)],
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserResponse:
    try:
        return await service.set_active(principal, user_id, body.active)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc
