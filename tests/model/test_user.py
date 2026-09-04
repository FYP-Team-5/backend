from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from app.dto import UserResponse
from app.model import Staff, Student, User, is_cross_service_id


def _common_fields() -> dict:
    now = datetime.now(UTC)
    return {
        "id": "1000001",
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
    assert staff.role == "instructor"


def test_discriminated_user_response_preserves_profile_type() -> None:
    adapter = TypeAdapter(UserResponse)

    student = adapter.validate_python(
        {**_common_fields(), "role": "student", "student_number": "S0001"}
    )
    staff = adapter.validate_python(
        {**_common_fields(), "id": "2000001", "role": "instructor", "staff_number": "E0001"}
    )

    assert isinstance(student, Student)
    assert isinstance(staff, Staff)


def test_numeric_user_id_is_compatible_with_grading_student_id_contract() -> None:
    user = Student(**_common_fields(), student_number="S0001")

    assert user.id.isdigit()
    assert is_cross_service_id(user.id)

    with pytest.raises(ValidationError):
        Student(
            **{**_common_fields(), "id": "student-1"},
            student_number="S0001",
        )
