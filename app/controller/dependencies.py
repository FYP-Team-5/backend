import re
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.dto import (
    UserResponse,
)
from app.service import (
    AttemptService,
    AuthenticationError,
    AuthService,
    CatalogService,
    GradingService,
    IdentityService,
    UserService,
)


def get_identity_service(request: Request) -> IdentityService:
    return request.app.state.identity_service

def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service

def get_user_service(request: Request) -> UserService:
    return request.app.state.user_service

def get_grading_service(request: Request) -> GradingService:
    return request.app.state.grading_service

def get_catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog_service

def get_attempt_service(request: Request) -> AttemptService:
    return request.app.state.attempt_service


bearer_scheme = HTTPBearer(auto_error=False)
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserResponse:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token is required.")
    try:
        _, user = await service.authenticate(credentials.credentials)
        return user
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Depends(api_key_header)],
) -> None:
    configured = request.app.state.settings.api_key
    if configured and (
        x_api_key is None or not secrets.compare_digest(x_api_key, configured)
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")

async def current_user_id(
    principal: Annotated[UserResponse, Depends(current_user)],
) -> str:
    """Attempt routes use this instead of trusting a caller-supplied header,
    so an attempt's owner is always whoever the bearer token authenticates
    as, not an id the caller can pick.
    """
    return principal.id