from __future__ import annotations

import asyncio
import secrets

from app.config import Settings
from app.db import PostgresUserRepository, UserNotFoundError
from app.model import (
    Staff,
    StaffRegistration,
    Student,
    StudentRegistration,
    TokenClaims,
    TokenResponse,
    UserResponse,
)
from app.service.security import InvalidTokenError, PasswordHasher, TokenManager


class AuthenticationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class StaffRegistrationError(PermissionError):
    pass


class UserService:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: PostgresUserRepository | None = None,
        password_hasher: PasswordHasher | None = None,
        token_manager: TokenManager | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or PostgresUserRepository(settings.database_url)
        self.password_hasher = password_hasher or PasswordHasher()
        self.tokens = token_manager or TokenManager(
            secret=settings.jwt_secret,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            expiry_minutes=settings.access_token_expiry_minutes,
        )

    async def initialize(self) -> None:
        await asyncio.to_thread(self.repository.initialize)

    async def close(self) -> None:
        await asyncio.to_thread(self.repository.close)

    async def health(self) -> bool:
        return await asyncio.to_thread(self.repository.health)

    async def register_student(self, request: StudentRegistration) -> Student:
        password_hash = await asyncio.to_thread(
            self.password_hasher.hash,
            request.password,
        )
        return await asyncio.to_thread(
            self.repository.create_student,
            email=request.email,
            full_name=request.full_name,
            student_number=request.student_number,
            password_hash=password_hash,
        )

    async def register_staff(
        self,
        request: StaffRegistration,
        registration_key: str | None,
    ) -> Staff:
        if registration_key is None or not secrets.compare_digest(
            registration_key,
            self.settings.staff_registration_key,
        ):
            raise StaffRegistrationError("Staff registration key is invalid.")
        password_hash = await asyncio.to_thread(
            self.password_hasher.hash,
            request.password,
        )
        return await asyncio.to_thread(
            self.repository.create_staff,
            email=request.email,
            full_name=request.full_name,
            staff_number=request.staff_number,
            password_hash=password_hash,
        )

    async def login(self, email: str, password: str) -> TokenResponse:
        try:
            user, password_hash = await asyncio.to_thread(
                self.repository.get_by_email,
                email,
            )
        except UserNotFoundError as exc:
            raise AuthenticationError("Email or password is invalid.") from exc
        password_matches = await asyncio.to_thread(
            self.password_hasher.verify,
            password,
            password_hash,
        )
        if not password_matches:
            raise AuthenticationError("Email or password is invalid.")
        if not user.active:
            raise AuthenticationError("User account is inactive.")
        institutional_number = (
            user.student_number if isinstance(user, Student) else user.staff_number
        )
        return TokenResponse(
            access_token=self.tokens.issue(user, institutional_number),
            expires_in=self.tokens.expires_in_seconds,
            user=user,
        )

    async def authenticate(self, token: str) -> tuple[TokenClaims, UserResponse]:
        try:
            claims = self.tokens.verify(token)
            user, _ = await asyncio.to_thread(self.repository.get, claims.sub)
        except (InvalidTokenError, UserNotFoundError) as exc:
            raise AuthenticationError("Bearer token is invalid or expired.") from exc
        if not user.active:
            raise AuthenticationError("User account is inactive.")
        if user.role != claims.role or user.email != claims.email:
            raise AuthenticationError("Bearer token no longer matches the user.")
        return claims, user

    async def list_users(
        self,
        principal: UserResponse,
        *,
        role: str | None = None,
    ) -> list[UserResponse]:
        self.require_staff(principal)
        return await asyncio.to_thread(self.repository.list, role=role)

    async def get_user(
        self,
        principal: UserResponse,
        user_id: str,
    ) -> UserResponse:
        if principal.id != user_id:
            self.require_staff(principal)
        user, _ = await asyncio.to_thread(self.repository.get, user_id)
        return user

    async def set_active(
        self,
        principal: UserResponse,
        user_id: str,
        active: bool,
    ) -> UserResponse:
        self.require_staff(principal)
        if principal.id == user_id and not active:
            raise AuthorizationError("Staff cannot deactivate their own account.")
        return await asyncio.to_thread(self.repository.set_active, user_id, active)

    @staticmethod
    def require_staff(principal: UserResponse) -> None:
        if not isinstance(principal, Staff):
            raise AuthorizationError("Staff role is required.")
