from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from app.model.assessment import Attempt, CriterionScore, Question, Response

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


def future_deadline(value: datetime) -> datetime:
    if value <= datetime.now(UTC):
        raise ValueError("End date must be in the future.")
    return value.astimezone(UTC)


class CourseCreate(BaseModel):
    course_code: str = Field(min_length=1, max_length=128)
    course_name: str = Field(min_length=1, max_length=300)


class CriteriaCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    score: float = Field(gt=0)


class RubricCreate(BaseModel):
    criteria: list[CriteriaCreate] = Field(min_length=1, max_length=100)
    model_answer: str | None = Field(default=None, max_length=10_000)


class ExampleCreate(BaseModel):
    band: Literal["excellent", "average", "poor"]
    example_answer: str = Field(min_length=1, max_length=10_000)
    score: float = Field(ge=0)


class GradingMethodUpdate(BaseModel):
    method: Literal["rubric", "fewshot"]


class QuestionCreate(BaseModel):
    external_id: str | None = Field(default=None, pattern=ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=10_000)
    max_score: float = Field(gt=0)
    score_increment: float = Field(gt=0)
    model_answer: str | None = Field(default=None, max_length=10_000)
    rubric: RubricCreate | None = None


class TestCreate(BaseModel):
    __test__ = False  # not a pytest test case; name matches the Test domain entity

    test_name: str = Field(min_length=1, max_length=300)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime
    max_attempts: int = Field(default=1, ge=1, le=100)
    questions: list[QuestionCreate] = Field(min_length=1, max_length=500)

    _future = field_validator("ends_at")(future_deadline)

    @model_validator(mode="after")
    def window_ordered(self) -> "TestCreate":
        if self.starts_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("End date must be after the start date.")
        return self


class TestDeadlineUpdate(BaseModel):
    ends_at: AwareDatetime

    _future = field_validator("ends_at")(future_deadline)


class ResponseGradeUpdate(BaseModel):
    response_id: str = Field(pattern=ID_PATTERN)
    score: float = Field(ge=0, allow_inf_nan=False)
    feedback: str = Field(max_length=10_000)
    criterion_scores: list[CriterionScore] | None = Field(
        default=None, min_length=1, max_length=100
    )


class SubmissionGradeUpdate(BaseModel):
    revision: int = Field(ge=0)
    responses: list[ResponseGradeUpdate] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_responses(self) -> "SubmissionGradeUpdate":
        ids = [item.response_id for item in self.responses]
        if len(ids) != len(set(ids)):
            raise ValueError("Each response may appear only once.")
        return self


class QuestionResponseSubmission(BaseModel):
    question_id: str = Field(pattern=ID_PATTERN)
    answer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_answer(self) -> "QuestionResponseSubmission":
        if not self.answer.strip():
            raise ValueError("answer must contain non-whitespace text")
        return self


class GradeAttemptRequest(BaseModel):
    responses: list[QuestionResponseSubmission] = Field(min_length=1, max_length=500)
    finalize: bool = True

    @model_validator(mode="after")
    def validate_response_ids(self) -> "GradeAttemptRequest":
        ids = [response.question_id for response in self.responses]
        if len(ids) != len(set(ids)):
            raise ValueError("each question may appear only once per request")
        return self


class FewShotGradeRequest(BaseModel):
    answer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_answer(self) -> "FewShotGradeRequest":
        if not self.answer.strip():
            raise ValueError("answer must contain non-whitespace text")
        return self


class AttemptGradeResponse(BaseModel):
    attempt: Attempt
    responses: list[Response]
    total_score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    percentage: float = Field(ge=0, le=100)
    completed_questions: int = Field(ge=0)
    total_questions: int = Field(ge=1)
    questions: list[Question] = Field(default_factory=list)
    can_edit: bool = False


class StudentSubmissionSummary(BaseModel):
    user_id: str
    full_name: str
    student_number: str
    latest_submission: Attempt | None = None
    latest_graded_submission: Attempt | None = None
    total_score: float | None = None
    max_score: float
