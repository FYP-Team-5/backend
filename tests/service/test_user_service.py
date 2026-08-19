import asyncio

import pytest

from app.dto import StaffRegistration, StudentRegistration
from app.service import (
    AuthenticationError,
    AuthorizationError,
    StaffRegistrationError,
    IdentityService,
)


def test_registration_login_and_authentication(service: IdentityService) -> None:
    student = asyncio.run(
        service.register_student(
            StudentRegistration(
                email=" STUDENT@EXAMPLE.EDU ",
                full_name="  Student   One ",
                student_number="S0001",
                password="a-secure-student-password",
            )
        )
    )

    result = asyncio.run(
        service.login("student@example.edu", "a-secure-student-password")
    )
    claims, authenticated = asyncio.run(service.authenticate(result.access_token))

    assert student.email == "student@example.edu"
    assert student.full_name == "Student One"
    assert result.expires_in == 1800
    assert claims.sub == student.id
    assert authenticated == student


def test_staff_registration_requires_bootstrap_key(service: IdentityService) -> None:
    request = StaffRegistration(
        email="staff@example.edu",
        full_name="Staff One",
        staff_number="E0001",
        password="a-secure-staff-password",
    )

    with pytest.raises(StaffRegistrationError):
        asyncio.run(service.register_staff(request, "wrong-key"))

    staff = asyncio.run(service.register_staff(request, "test-staff-registration-key"))
    assert staff.role == "staff"


def test_authorization_and_inactive_account(service: IdentityService) -> None:
    student = asyncio.run(
        service.register_student(
            StudentRegistration(
                email="student@example.edu",
                full_name="Student One",
                student_number="S0001",
                password="a-secure-student-password",
            )
        )
    )
    staff = asyncio.run(
        service.register_staff(
            StaffRegistration(
                email="staff@example.edu",
                full_name="Staff One",
                staff_number="E0001",
                password="a-secure-staff-password",
            ),
            "test-staff-registration-key",
        )
    )

    with pytest.raises(AuthorizationError):
        asyncio.run(service.list_users(student))
    assert len(asyncio.run(service.list_users(staff))) == 2
    with pytest.raises(AuthorizationError, match="own account"):
        asyncio.run(service.set_active(staff, staff.id, False))

    token = asyncio.run(
        service.login(student.email, "a-secure-student-password")
    ).access_token
    asyncio.run(service.set_active(staff, student.id, False))
    with pytest.raises(AuthenticationError, match="inactive"):
        asyncio.run(service.login(student.email, "a-secure-student-password"))
    with pytest.raises(AuthenticationError, match="inactive"):
        asyncio.run(service.authenticate(token))
