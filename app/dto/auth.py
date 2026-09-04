from typing import Literal

from pydantic import BaseModel, Field

from app.model.identifiers import USER_ID_PATTERN


class TokenClaims(BaseModel):
    sub: str = Field(pattern=USER_ID_PATTERN)
    role: Literal["student", "instructor"]
    email: str
    institutional_number: str
    iss: str
    aud: str
    iat: int
    exp: int
    jti: str
