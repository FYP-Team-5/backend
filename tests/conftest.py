from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.config import Settings
from app.db import (
    PostgresGradingRepository,
    PostgresUserRepository,
)
from app.main import create_app
from app.service import GradingService, IdentityService, LocalLLMClient
from app.service.dataset_service import DatasetService
from tests.dataset_fakes import MemoryDatasetStorage


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        staff_registration_key="test-staff-registration-key",
        access_token_expiry_minutes=30,
    )


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    """Share a file database while keeping worker and request connections separate."""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'api.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
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
def dataset_storage():
    return MemoryDatasetStorage()


@pytest.fixture
def dataset_service(settings, grading_repository, dataset_storage):
    settings.dataset_poll_seconds = 0.05
    return DatasetService(settings, grading_repository, storage=dataset_storage)


@pytest.fixture
def client(
    settings: Settings,
    identity_service: IdentityService,
    grading_service: GradingService,
    dataset_service: DatasetService,
) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        identity_service=identity_service,
        grading_service=grading_service,
        dataset_service=dataset_service,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def instructor_headers(client: TestClient) -> dict[str, str]:
    profile = {
        "email": "instructor@example.edu",
        "full_name": "Instructor",
        "staff_number": "T1",
        "password": "test-password-long-enough",
    }
    response = client.post(
        "/api/v1/auth/register/staff",
        json=profile,
        headers={"X-Staff-Registration-Key": "test-staff-registration-key"},
    )
    assert response.status_code == 201
    token = client.post(
        "/api/v1/auth/login",
        json={
            "email": profile["email"],
            "password": profile["password"],
        },
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
