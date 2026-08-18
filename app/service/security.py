from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.model import TokenClaims, User

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64


class InvalidTokenError(ValueError):
    pass


class PasswordHasher:
    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
        )
        return "$".join(
            (
                "scrypt",
                str(SCRYPT_N),
                str(SCRYPT_R),
                str(SCRYPT_P),
                _encode(salt),
                _encode(digest),
            )
        )

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt, expected = encoded.split("$", 5)
            if algorithm != "scrypt":
                return False
            if (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
                return False
            expected_bytes = _decode(expected)
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_decode(salt),
                n=SCRYPT_N,
                r=SCRYPT_R,
                p=SCRYPT_P,
                dklen=len(expected_bytes),
            )
        except (ValueError, TypeError, binascii.Error):
            return False
        return hmac.compare_digest(actual, expected_bytes)


class TokenManager:
    def __init__(
        self,
        *,
        secret: str,
        issuer: str,
        audience: str,
        expiry_minutes: int,
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT secret must contain at least 32 bytes.")
        self.secret = secret.encode("utf-8")
        self.issuer = issuer
        self.audience = audience
        self.expiry = timedelta(minutes=expiry_minutes)

    @property
    def expires_in_seconds(self) -> int:
        return int(self.expiry.total_seconds())

    def issue(self, user: User, institutional_number: str) -> str:
        now = datetime.now(UTC)
        claims = TokenClaims(
            sub=user.id,
            role=user.role,
            email=user.email,
            institutional_number=institutional_number,
            iss=self.issuer,
            aud=self.audience,
            iat=int(now.timestamp()),
            exp=int((now + self.expiry).timestamp()),
            jti=str(uuid.uuid4()),
        )
        header = {"alg": "HS256", "typ": "JWT"}
        encoded_header = _encode_json(header)
        encoded_payload = _encode_json(claims.model_dump())
        signing_input = f"{encoded_header}.{encoded_payload}".encode()
        signature = hmac.new(self.secret, signing_input, hashlib.sha256).digest()
        return f"{encoded_header}.{encoded_payload}.{_encode(signature)}"

    def verify(self, token: str, *, now: datetime | None = None) -> TokenClaims:
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            signing_input = f"{encoded_header}.{encoded_payload}".encode()
            expected = hmac.new(self.secret, signing_input, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _decode(encoded_signature)):
                raise InvalidTokenError("Token signature is invalid.")
            header = _decode_json(encoded_header)
            payload = _decode_json(encoded_payload)
            if header != {"alg": "HS256", "typ": "JWT"}:
                raise InvalidTokenError("Token header is invalid.")
            claims = TokenClaims.model_validate(payload)
        except (
            ValueError,
            TypeError,
            binascii.Error,
            UnicodeDecodeError,
            ValidationError,
        ) as exc:
            if isinstance(exc, InvalidTokenError):
                raise
            raise InvalidTokenError("Token is malformed.") from exc

        current_timestamp = int((now or datetime.now(UTC)).timestamp())
        if claims.iss != self.issuer or claims.aud != self.audience:
            raise InvalidTokenError("Token issuer or audience is invalid.")
        if claims.iat > current_timestamp + 30:
            raise InvalidTokenError("Token issue time is invalid.")
        if claims.exp <= current_timestamp:
            raise InvalidTokenError("Token has expired.")
        return claims


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_json(value: dict[str, Any]) -> str:
    return _encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def _decode_json(value: str) -> dict[str, Any]:
    decoded = json.loads(_decode(value))
    if not isinstance(decoded, dict):
        raise TypeError("JWT section must be an object.")
    return decoded
