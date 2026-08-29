"""Routes under /auth."""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException

from app.controller.dependencies import get_auth_service
from app.db import UserConflictError, UserStoreError
from app.dto import (
    LoginRequest,
    StaffRegistration,
    StudentRegistration,
    TokenResponse,
)
from app.model import Staff, Student
from app.service import (
    AuthenticationError,
    AuthService,
    StaffRegistrationError,
)

auth_router = APIRouter(prefix="/auth", tags=["authentication"])

@auth_router.post(
    "/register/student",
    response_model=Student,
    status_code=201,
)
async def register_student(
    body: StudentRegistration,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> Student:
    try:
        return await service.register_student(body)
    except UserConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc

@auth_router.post(
    "/register/staff",
    response_model=Staff,
    status_code=201,
)
async def register_staff(
    body: StaffRegistration,
    service: Annotated[AuthService, Depends(get_auth_service)],
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


@auth_router.post(
    "/login",
    response_model=TokenResponse,
)
async def login(
    body: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    try:
        return await service.login(body.email, body.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except UserStoreError as exc:
        raise HTTPException(status_code=502, detail="User database failed.") from exc