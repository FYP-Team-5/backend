from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Course(BaseModel):
    id: str
    course_code: str
    course_name: str
    created_at: datetime


class Criteria(BaseModel):
    id: str
    rubric_id: str
    description: str
    score: float = Field(gt=0, allow_inf_nan=False)


class Rubric(BaseModel):
    id: str
    criteria: list[Criteria]


class Example(BaseModel):
    id: str
    question_id: str
    band: Literal["excellent", "average", "poor"]
    example_answer: str
    score: float = Field(ge=0, allow_inf_nan=False)


class Question(BaseModel):
    id: str
    test_id: str
    external_id: str | None = None
    prompt: str
    max_score: float = Field(gt=0, allow_inf_nan=False)
    score_increment: float = Field(gt=0, allow_inf_nan=False)
    model_answer: str | None = None
    rubric: Rubric | None = None
    examples: list[Example] = Field(default_factory=list)
    grading_method: Literal["rubric", "fewshot"] | None = None
    position: int = Field(ge=0)


class Test(BaseModel):
    id: str
    course_id: str
    test_name: str
    max_attempts: int
    questions: list[Question]
    created_at: datetime
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    ended_at: datetime | None = None
    ended_by: str | None = None

    _utc = field_validator("starts_at", "ends_at", "ended_at")(utc_datetime)

    @computed_field
    @property
    def is_closed(self) -> bool:
        # Open only inside the [starts_at, ends_at) window, unless an
        # instructor ended the test early. A missing start date means the
        # test is open from creation.
        if self.ended_at is not None:
            return True
        now = datetime.now(UTC)
        if self.starts_at is not None and now < self.starts_at:
            return True
        return self.ends_at is not None and now >= self.ends_at


class Attempt(BaseModel):
    id: str
    test_id: str
    user_id: str
    attempt_number: int = Field(ge=1)
    status: Literal["in_progress", "grading", "graded", "failed"]
    started_at: datetime
    submitted_at: datetime | None = None
    submission_version: int = 0
    revision: int = 0
    graded_at: datetime | None = None
    error: str | None = None

    _utc = field_validator("started_at", "submitted_at", "graded_at")(utc_datetime)


class CriteriaMet(BaseModel):
    id: str
    criteria_id: str
    is_met: bool


class CriterionScore(BaseModel):
    criteria_id: str
    score: float = Field(ge=0, allow_inf_nan=False)


class Response(BaseModel):
    id: str
    attempt_id: str
    question_id: str
    answer: str
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    feedback: str | None = None
    criteria_met: list[CriteriaMet]
    criterion_scores: list[CriterionScore] | None = None
    original_score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    original_feedback: str | None = None
    overridden: bool = False
    edited_by: str | None = None
    edited_at: datetime | None = None
