from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import (
    PostgresGradingRepository,
    PostgresUserRepository,
)
from app.main import create_app
from app.service import GradingService, IdentityService, LocalLLMClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        staff_registration_key="test-staff-registration-key",
        access_token_expiry_minutes=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """One engine shared by both repositories: anonymous in-memory SQLite
    gives every separate engine its own private, empty database."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def repository(engine: Engine) -> PostgresUserRepository:
    store = PostgresUserRepository(engine=engine)
    store.initialize()
    return store


@pytest.fixture
def grading_repository(engine: Engine) -> PostgresGradingRepository:
    return PostgresGradingRepository(engine=engine)


@pytest.fixture
def identity_service(
    settings: Settings,
    repository: PostgresUserRepository,
) -> IdentityService:
    return IdentityService(settings, repository=repository)


@pytest.fixture
def service(identity_service: IdentityService) -> IdentityService:
    """Alias for tests/service/test_user_service.py."""
    return identity_service


@pytest.fixture
def llm_client() -> LocalLLMClient:
    client = MagicMock(spec=LocalLLMClient)
    client.close = AsyncMock()
    client.health = AsyncMock(return_value=True)
    client.grade = AsyncMock()
    return client


@pytest.fixture
def grading_service(
    settings: Settings,
    grading_repository: PostgresGradingRepository,
    llm_client: LocalLLMClient,
) -> GradingService:
    return GradingService(
        settings,
        grading_store=grading_repository,
        llm_client=llm_client,
    )


@pytest.fixture
def client(
    settings: Settings,
    identity_service: IdentityService,
    grading_service: GradingService,
) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        identity_service=identity_service,
        grading_service=grading_service,
    )
    with TestClient(app) as test_client:
        yield test_client
