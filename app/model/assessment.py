from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Course(BaseModel):
    id: str
    course_code: str
    course_name: str
    created_at: datetime

class Criteria(BaseModel):
    id: str
    rubric_id: str
    description: str
    score: float = Field(gt=0)

class Rubric(BaseModel):
    id: str
    criteria: list[Criteria]
    created_at: datetime

class Question(BaseModel):
    id: str
    test_id: str
    prompt: str
    max_score: float = Field(gt=0)
    score_increment: float = Field(gt=0)
    rubric_id: str
    position: int = Field(ge=0)

class Test(BaseModel):
    id: str
    course_id: str
    test_name: str
    max_attempts: int
    questions: list[Question]
    created_at: datetime

class Attempt(BaseModel):
    id: str
    test_id: str
    user_id: str
    attempt_number: int = Field(ge=1)
    status: Literal["in_progress", "graded", "failed"]
    started_at: datetime
    graded_at: datetime | None = None
    error: str | None = None

class CriteriaMet(BaseModel):
    id: str
    criteria_id: str
    is_met: bool

class Response(BaseModel):
    id: str
    attempt_id: str
    question_id: str
    answer: str
    score: float = Field(ge=0)
    feedback: str | None = None
    criteria_met: list[CriteriaMet]
