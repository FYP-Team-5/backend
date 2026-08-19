from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from app.model.user import (
    INSTITUTIONAL_NUMBER_PATTERN,
    Staff,
    Student,
    _normalize_email,
)

UserResponse = Annotated[Student | Staff, Field(discriminator="role")]

class RegistrationBase(BaseModel):
    email: str
    full_name: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=12, max_length=256)
    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str: return _normalize_email(value)
    @field_validator("full_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized: raise ValueError("full name is required")
        return normalized

class StudentRegistration(RegistrationBase):
    student_number: str = Field(pattern=INSTITUTIONAL_NUMBER_PATTERN)

class StaffRegistration(RegistrationBase):
    staff_number: str = Field(pattern=INSTITUTIONAL_NUMBER_PATTERN)

class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=256)
    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str: return _normalize_email(value)

class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0)
    user: UserResponse

class UserStatusUpdate(BaseModel):
    active: bool
