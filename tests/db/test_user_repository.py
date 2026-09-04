import pytest

from app.db import PostgresUserRepository, UserConflictError, UserNotFoundError
from app.model import Staff, Student


def test_joined_profiles_support_student_and_staff_inheritance(
    repository: PostgresUserRepository,
) -> None:
    student = repository.create_student(
        email="student@example.edu",
        full_name="Student One",
        student_number="S0001",
        password_hash="student-hash",
    )
    repository.create_staff(
        email="staff@example.edu",
        full_name="Staff One",
        staff_number="E0001",
        password_hash="staff-hash",
    )

    loaded_student, student_hash = repository.get(student.id)
    loaded_staff, staff_hash = repository.get_by_email("staff@example.edu")

    assert isinstance(loaded_student, Student)
    assert student.id == "1000001"
    assert loaded_staff.id == "2000001"
    assert student.id.isdigit() and loaded_staff.id.isdigit()
    assert student.id != loaded_staff.id
    assert loaded_student.student_number == "S0001"
    assert student_hash == "student-hash"
    assert isinstance(loaded_staff, Staff)
    assert loaded_staff.staff_number == "E0001"
    assert staff_hash == "staff-hash"
    assert repository.list(role="student") == [loaded_student]
    assert len(repository.list()) == 2


def test_email_and_institutional_numbers_are_unique(
    repository: PostgresUserRepository,
) -> None:
    repository.create_student(
        email="student@example.edu",
        full_name="Student One",
        student_number="S0001",
        password_hash="hash",
    )

    with pytest.raises(UserConflictError):
        repository.create_staff(
            email="student@example.edu",
            full_name="Staff One",
            staff_number="E0001",
            password_hash="hash",
        )
    with pytest.raises(UserConflictError):
        repository.create_student(
            email="student-two@example.edu",
            full_name="Student Two",
            student_number="S0001",
            password_hash="hash",
        )


def test_status_update_and_missing_user(repository: PostgresUserRepository) -> None:
    student = repository.create_student(
        email="student@example.edu",
        full_name="Student One",
        student_number="S0001",
        password_hash="hash",
    )

    updated = repository.set_active(student.id, False)

    assert updated.active is False
    with pytest.raises(UserNotFoundError):
        repository.get("missing-user")
