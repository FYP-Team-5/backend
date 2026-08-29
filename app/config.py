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

    app_name: str = "Student Answer Grading Service"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    api_key: str | None = None
    cors_origins: str = "*"

    rag_database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"
    database_url: str = "postgresql+psycopg://user:change-me@localhost:5433/user"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "rubric_chunks"
    qdrant_timeout_seconds: float = Field(default=30, gt=0, le=300)

    llm_url: str = "http://localhost:11434/v1/chat/completions"
    llm_model: str = "local-model"
    llm_api_key: str | None = None
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)
    llm_max_retries: int = Field(default=1, ge=0, le=10)
    llm_temperature: float = Field(default=0, ge=0, le=2)
    llm_max_tokens: int = Field(default=2000, ge=128, le=32_768)

    max_answer_characters: int = Field(default=50_000, ge=1_000, le=1_000_000)

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
