from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.dto import TokenClaims
from app.model import Student
from app.service import TokenManager


def test_token_subject_is_the_same_stable_user_id() -> None:
    now = datetime.now(UTC)
    student = Student(
        id="60f1ec55-f74e-4924-a9ee-1d79f902f846",
        email="student@example.edu",
        full_name="Student One",
        student_number="S0001",
        active=True,
        created_at=now,
        updated_at=now,
    )
    manager = TokenManager(
        secret="a-test-secret-that-is-at-least-32-bytes",
        issuer="user-service",
        audience="assessment-services",
        expiry_minutes=30,
    )

    claims = manager.verify(manager.issue(student, student.student_number))

    assert claims.sub == student.id
    assert claims.role == "student"
    assert claims.institutional_number == "S0001"


def test_token_subject_rejects_non_user_identifiers() -> None:
    now = int(datetime.now(UTC).timestamp())

    with pytest.raises(ValidationError):
        TokenClaims(
            sub="student-1",
            role="student",
            email="student@example.edu",
            institutional_number="S0001",
            iss="user-service",
            aud="assessment-services",
            iat=now,
            exp=now + 1800,
            jti="token-id",
        )
