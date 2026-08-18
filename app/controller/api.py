from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import UserConflictError, UserNotFoundError, UserStoreError
from app.model import (
    HealthResponse,
    LoginRequest,
    Staff,
    StaffRegistration,
    Student,
    StudentRegistration,
    TokenResponse,
    UserResponse,
    UserStatusUpdate,
)
from app.service import (
    AuthenticationError,
    AuthorizationError,
    StaffRegistrationError,
    UserService,
)


def get_service(request: Request) -> UserService:
    return request.app.state.user_service


bearer_scheme = HTTPBearer(auto_error=False)


async def current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    service: Annotated[UserService, Depends(get_service)],
) -> UserResponse:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token is required.")
    try:
        _, user = await service.authenticate(credentials.credentials)
        return user
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


health_router = APIRouter(tags=["health"])
router = APIRouter()


@health_router.get("/health", response_model=HealthResponse)
async def health(
    service: Annotated[UserService, Depends(get_service)],
) -> HealthResponse:
    if not await service.health():
        raise HTTPException(status_code=503, detail={"postgres": "unavailable"})
    return HealthResponse(status="ok", postgres="ok")


@router.post(
    "/auth/register/student",
    response_model=Student,
    status_code=201,
    tags=["authentication"],
)
async def register_student(
    body: StudentRegistration,
    service: Annotated[UserService, Depends(get_service)],
) -> Student:
    try:
        return await service.register_student(body)
    except UserConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@router.post(
    "/auth/register/staff",
    response_model=Staff,
    status_code=201,
    tags=["authentication"],
)
async def register_staff(
    body: StaffRegistration,
    service: Annotated[UserService, Depends(get_service)],
    registration_key: Annotated[
        str | None,
        Header(alias="X-Staff-Registration-Key"),
    ] = None,
) -> Staff:
    try:
        return await service.register_staff(body, registration_key)
    except StaffRegistrationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    tags=["authentication"],
)
async def login(
    body: LoginRequest,
    service: Annotated[UserService, Depends(get_service)],
) -> TokenResponse:
    try:
        return await service.login(body.email, body.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@router.get("/users/me", response_model=UserResponse, tags=["users"])
async def get_me(
    principal: Annotated[UserResponse, Depends(current_user)],
) -> UserResponse:
    return principal


@router.get("/users", response_model=list[UserResponse], tags=["users"])
async def list_users(
    principal: Annotated[UserResponse, Depends(current_user)],
    service: Annotated[UserService, Depends(get_service)],
    role: Annotated[Literal["student", "staff"] | None, Query()] = None,
) -> list[UserResponse]:
    try:
        return await service.list_users(principal, role=role)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@router.get("/users/{user_id}", response_model=UserResponse, tags=["users"])
async def get_user(
    user_id: str,
    principal: Annotated[UserResponse, Depends(current_user)],
    service: Annotated[UserService, Depends(get_service)],
) -> UserResponse:
    try:
        return await service.get_user(principal, user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc


@router.patch(
    "/users/{user_id}/status",
    response_model=UserResponse,
    tags=["users"],
)
async def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    principal: Annotated[UserResponse, Depends(current_user)],
    service: Annotated[UserService, Depends(get_service)],
) -> UserResponse:
    try:
        return await service.set_active(principal, user_id, body.active)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc
