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
    training_callback_token: str | None = None
    training_hf_repo: str = "SmuFypTeam5/GradingQlora"
    training_hf_token: str | None = None
    training_adapter_directory: str = "/mnt/llm-data/adapters"
    training_max_lora_rank: int = Field(default=64, ge=1, le=512)

    database_url: str = "postgresql+psycopg://user:change-me@localhost:5433/user"

    llm_url: str = "http://localhost:11434/v1/chat/completions"
    llm_model: str = "local-model"
    llm_api_key: str | None = None
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)
    llm_max_retries: int = Field(default=1, ge=0, le=10)
    llm_temperature: float = Field(default=0, ge=0, le=2)
    llm_max_tokens: int = Field(default=2048, ge=128, le=32_768)

    max_answer_characters: int = Field(default=50_000, ge=1_000, le=1_000_000)
    grading_poll_seconds: float = Field(default=1, gt=0, le=60)
    grading_lease_seconds: int = Field(default=60, ge=3, le=3600)
    grading_max_retries: int = Field(default=3, ge=0, le=10)
    grading_retry_seconds: float = Field(default=5, ge=0, le=3600)

    dataset_s3_endpoint: str = "http://localhost:8333"
    dataset_s3_bucket: str = "llm-training"
    dataset_s3_access_key: str = "fyp-training"
    dataset_s3_secret_key: str = "development-only-training-secret"
    dataset_s3_region: str = "us-east-1"
    dataset_upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    dataset_poll_seconds: float = Field(default=5, gt=0, le=3600)
    dataset_lease_seconds: int = Field(default=120, ge=3, le=3600)
    dataset_max_retries: int = Field(default=3, ge=0, le=10)
    dataset_retry_seconds: float = Field(default=5, ge=0, le=3600)

    jwt_secret: str = "development-only-change-me-at-least-32-bytes"
    jwt_issuer: str = "user-service"
    jwt_audience: str = "assessment-services"
    access_token_expiry_minutes: int = Field(default=30, ge=1, le=1440)
    staff_registration_key: str = "development-staff-bootstrap-key"
    seed_demo_users: bool = True

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
