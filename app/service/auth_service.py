from app.dto import StaffRegistration, StudentRegistration, TokenClaims, TokenResponse, UserResponse
from app.model import Staff, Student


class AuthService:
    """Authentication and account-registration use cases."""

    def __init__(self, core) -> None:
        self.core = core

    async def register_student(self, request: StudentRegistration) -> Student:
        return await self.core.register_student(request)

    async def register_staff(self, request: StaffRegistration, key: str | None) -> Staff:
        return await self.core.register_staff(request, key)

    async def login(self, email: str, password: str) -> TokenResponse:
        return await self.core.login(email, password)

    async def authenticate(self, token: str) -> tuple[TokenClaims, UserResponse]:
        return await self.core.authenticate(token)
