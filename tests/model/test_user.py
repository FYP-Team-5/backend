from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from app.dto import UserResponse
from app.model import Staff, Student, User, is_cross_service_id


def _common_fields() -> dict:
    now = datetime.now(UTC)
    return {
        "id": "60f1ec55-f74e-4924-a9ee-1d79f902f846",
        "email": " PERSON@EXAMPLE.EDU ",
        "full_name": "Person One",
        "active": True,
        "created_at": now,
        "updated_at": now,
    }


def test_student_and_staff_inherit_from_user() -> None:
    student = Student(**_common_fields(), student_number="S0001")
    staff = Staff(**_common_fields(), staff_number="E0001")

    assert isinstance(student, User)
    assert isinstance(staff, User)
    assert student.email == "person@example.edu"
    assert student.role == "student"
    assert staff.role == "staff"


def test_discriminated_user_response_preserves_profile_type() -> None:
    adapter = TypeAdapter(UserResponse)

    student = adapter.validate_python(
        {**_common_fields(), "role": "student", "student_number": "S0001"}
    )
    staff = adapter.validate_python(
        {**_common_fields(), "role": "staff", "staff_number": "E0001"}
    )

    assert isinstance(student, Student)
    assert isinstance(staff, Staff)


def test_user_uuid_is_compatible_with_grading_student_id_contract() -> None:
    user = Student(**_common_fields(), student_number="S0001")

    assert len(user.id) == 36
    assert is_cross_service_id(user.id)

    with pytest.raises(ValidationError):
        Student(
            **{**_common_fields(), "id": "student-1"},
            student_number="S0001",
        )
