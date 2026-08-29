from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Course(BaseModel):
    id: str
    title: str
    created_at: datetime

class Question(BaseModel):
    id: str
    prompt: str
    max_score: float = Field(gt=0)
    rubric_chunk_indexes: list[int] = Field(default_factory=list)
    position: int = Field(ge=0)

class Exam(BaseModel):
    id: str
    course_id: str
    title: str
    type: Literal["exam", "quiz"]
    max_attempts: int
    rubric_id: str
    questions: list[Question]
    created_at: datetime

class Attempt(BaseModel):
    id: str
    exam_id: str
    student_id: str
    attempt_number: int = Field(ge=1)
    status: Literal["in_progress", "graded", "failed"]
    rubric_id: str
    rubric_version: str
    started_at: datetime
    graded_at: datetime | None = None
    error: str | None = None

class QuestionGrade(BaseModel):
    question_id: str
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    percentage: float = Field(ge=0, le=100)
    feedback: str
    criteria: list[dict]
    rubric_chunk_ids: list[str]
