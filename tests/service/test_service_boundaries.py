import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from app.dto import LoginRequest, StaffRegistration, StudentRegistration, UserResponse
from app.model import Student
from app.service import AuthService, UserService


def _student() -> Student:
    now = datetime.now(UTC)
    return Student(id="60f1ec55-f74e-4924-a9ee-1d79f902f846", email="s@example.edu", full_name="S", active=True, created_at=now, updated_at=now, student_number="S1")


class CoreSpy:
    def __init__(self) -> None:
        self.calls = []
        self.student = _student()

    async def register_student(self, request): self.calls.append(("register_student", request)); return self.student
    async def register_staff(self, request, key): self.calls.append(("register_staff", request, key)); return request
    async def login(self, email, password): self.calls.append(("login", email, password)); return "token"
    async def authenticate(self, token): self.calls.append(("authenticate", token)); return "claims", self.student
    async def list_users(self, principal, *, role=None): self.calls.append(("list", principal, role)); return [self.student]
    async def get_user(self, principal, user_id): self.calls.append(("get", principal, user_id)); return self.student
    async def set_active(self, principal, user_id, active): self.calls.append(("active", principal, user_id, active)); return self.student


def test_focused_services_delegate_only_their_use_cases() -> None:
    core = CoreSpy()
    auth, users = AuthService(core), UserService(core)
    student_request = StudentRegistration(email="S@EXAMPLE.EDU", full_name=" Student ", password="x" * 12, student_number="S1")
    staff_request = StaffRegistration(email="t@example.edu", full_name="Staff", password="x" * 12, staff_number="T1")
    assert asyncio.run(auth.register_student(student_request)) is core.student
    asyncio.run(auth.register_staff(staff_request, "key"))
    assert asyncio.run(auth.login("s@example.edu", "password")) == "token"
    assert asyncio.run(auth.authenticate("jwt"))[1] is core.student
    assert asyncio.run(users.list_users(core.student, role="student")) == [core.student]
    assert asyncio.run(users.get_user(core.student, core.student.id)) is core.student
    assert asyncio.run(users.set_active(core.student, core.student.id, False)) is core.student
    assert [call[0] for call in core.calls] == ["register_student", "register_staff", "login", "authenticate", "list", "get", "active"]


@pytest.mark.parametrize("password", ["", "x" * 11, "x" * 257])
def test_registration_password_boundaries(password: str) -> None:
    with pytest.raises(ValidationError):
        StudentRegistration(email="s@example.edu", full_name="Student", password=password, student_number="S1")


@pytest.mark.parametrize("email", ["missing-at.example.edu", "a@nodot", "a @example.edu", "@example.edu"])
def test_login_rejects_malformed_email(email: str) -> None:
    with pytest.raises(ValidationError):
        LoginRequest(email=email, password="x")


def test_user_response_rejects_role_profile_mismatch() -> None:
    payload = _student().model_dump() | {"role": "staff"}
    with pytest.raises(ValidationError):
        TypeAdapter(UserResponse).validate_python(payload)
