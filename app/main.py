import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.config import Settings, get_settings
from app.controller import auth_router, health_router, users_router
from app.service import AuthService, IdentityService, UserService

OPENAPI_TAGS = [
    {"name": "health", "description": "User database readiness."},
    {
        "name": "authentication",
        "description": "Register student/staff accounts and issue access tokens.",
    },
    {"name": "users", "description": "Inspect and administer user profiles."},
]


def create_app(
    *,
    settings: Settings | None = None,
    service: IdentityService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    identity_service = service or IdentityService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        app.state.settings = settings
        app.state.identity_service = identity_service
        await identity_service.initialize()
        try:
            yield
        finally:
            await identity_service.close()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Own student/staff identities, verify credentials, and issue bearer "
            "tokens for the assessment services."
        ),
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        swagger_ui_parameters={"displayRequestDuration": True, "filter": True},
    )
    app.state.settings = settings
    app.state.identity_service = identity_service
    app.state.auth_service = AuthService(identity_service)
    app.state.user_service = UserService(identity_service)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=settings.allowed_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(auth_router, prefix=settings.api_v1_prefix)
    app.include_router(users_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app


app = create_app()
