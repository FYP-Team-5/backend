from datetime import UTC, datetime, timedelta

import pytest

from app.model import Student
from app.service import InvalidTokenError, PasswordHasher, TokenManager


def _student() -> Student:
    now = datetime.now(UTC)
    return Student(
            id="1000001",
        email="student@example.edu",
        full_name="Test Student",
        student_number="S0001",
        active=True,
        created_at=now,
        updated_at=now,
    )


def test_password_hash_is_salted_and_verifiable() -> None:
    hasher = PasswordHasher()

    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")

    assert first != second
    assert hasher.verify("correct horse battery staple", first)
    assert not hasher.verify("wrong password", first)
    assert not hasher.verify("password", "not-a-supported-hash")


@pytest.mark.parametrize(
    "encoded",
    [
        "bcrypt$16384$8$1$salt$digest",
        "scrypt$1$8$1$salt$digest",
        "scrypt$not-an-int$8$1$salt$digest",
        "scrypt$16384$8$1$%%%$%%%",
    ],
)
def test_password_verification_rejects_mutated_hash_formats(encoded: str) -> None:
    assert not PasswordHasher().verify("password", encoded)


def test_token_manager_rejects_weak_secret_boundary() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TokenManager(secret="x" * 31, issuer="issuer", audience="audience", expiry_minutes=1)


def test_token_round_trip_and_claims() -> None:
    manager = TokenManager(
        secret="a-test-secret-that-is-at-least-32-bytes",
        issuer="user-service",
        audience="assessment-services",
        expiry_minutes=15,
    )

    token = manager.issue(_student(), "S0001")
    claims = manager.verify(token)

    assert claims.sub == "1000001"
    assert claims.role == "student"
    assert claims.institutional_number == "S0001"
    assert claims.aud == "assessment-services"


def test_rejects_modified_expired_and_wrong_audience_tokens() -> None:
    manager = TokenManager(
        secret="a-test-secret-that-is-at-least-32-bytes",
        issuer="user-service",
        audience="assessment-services",
        expiry_minutes=15,
    )
    token = manager.issue(_student(), "S0001")
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"

    with pytest.raises(InvalidTokenError, match="signature"):
        manager.verify(f"{header}.{payload}.{replacement}{signature[1:]}")

    with pytest.raises(InvalidTokenError, match="expired"):
        manager.verify(token, now=datetime.now(UTC) + timedelta(minutes=16))

    other_audience = TokenManager(
        secret="a-test-secret-that-is-at-least-32-bytes",
        issuer="user-service",
        audience="another-service",
        expiry_minutes=15,
    )
    with pytest.raises(InvalidTokenError, match="issuer or audience"):
        other_audience.verify(token)


@pytest.mark.parametrize("token", ["", "one.two", "one.two.three.four", "%%%.%%%.%%%"])
def test_rejects_malformed_token_boundaries(token: str) -> None:
    manager = TokenManager(
        secret="a-test-secret-that-is-at-least-32-bytes",
        issuer="user-service",
        audience="assessment-services",
        expiry_minutes=15,
    )
    with pytest.raises(InvalidTokenError, match="malformed|signature"):
        manager.verify(token)
