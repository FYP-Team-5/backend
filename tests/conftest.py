from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import PostgresUserRepository
from app.main import create_app
from app.service import UserService


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        staff_registration_key="test-staff-registration-key",
        access_token_expiry_minutes=30,
    )


@pytest.fixture
def repository() -> Iterator[PostgresUserRepository]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = PostgresUserRepository(engine=engine)
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def service(
    settings: Settings,
    repository: PostgresUserRepository,
) -> UserService:
    return UserService(settings, repository=repository)


@pytest.fixture
def client(settings: Settings, service: UserService) -> Iterator[TestClient]:
    with TestClient(create_app(settings=settings, service=service)) as test_client:
        yield test_client
