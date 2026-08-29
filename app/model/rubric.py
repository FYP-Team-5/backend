from typing import Literal

from pydantic import BaseModel, Field


class RubricMetadata(BaseModel):
    id: str
    document_id: str
    version: str
    course_id: str | None = None
    exam_id: str | None = None
    processed: bool
    processing_status: Literal["processing", "completed", "failed"]
    processing_error: str | None = None
    archived: bool = False
    chunk_count: int = Field(ge=0)
    chunk_ids: list[str]
