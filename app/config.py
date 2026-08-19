from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    app_name: str = "User Identity Service"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    cors_origins: str = "*"

    database_url: str = "postgresql+psycopg://user:change-me@localhost:5433/user"

    jwt_secret: str = "development-only-change-me-at-least-32-bytes"
    jwt_issuer: str = "user-service"
    jwt_audience: str = "assessment-services"
    access_token_expiry_minutes: int = Field(default=30, ge=1, le=1440)
    staff_registration_key: str = "development-staff-bootstrap-key"

    @property
    def allowed_origins(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
