from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.model.identifiers import USER_ID_PATTERN

INSTITUTIONAL_NUMBER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def _normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if (
        len(normalized) > 320
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("a valid email address is required")
    local, domain = normalized.split("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("a valid email address is required")
    return normalized


class User(BaseModel):
    """Parent model shared by every authenticated user type."""

    id: str = Field(pattern=USER_ID_PATTERN)
    email: str
    full_name: str
    role: Literal["student", "instructor"]
    active: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)


class Student(User):
    role: Literal["student"] = "student"
    student_number: str = Field(pattern=INSTITUTIONAL_NUMBER_PATTERN)


class Staff(User):
    role: Literal["instructor"] = "instructor"
    staff_number: str = Field(pattern=INSTITUTIONAL_NUMBER_PATTERN)

