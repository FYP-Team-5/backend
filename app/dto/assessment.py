from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.model.assessment import Attempt, Response

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"

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
    max_attempts: int = Field(default=1, ge=1, le=100)
    questions: list[QuestionCreate] = Field(min_length=1, max_length=500)

class QuestionResponseSubmission(BaseModel):
    question_id: str = Field(pattern=ID_PATTERN)
    answer: str = Field(min_length=1)
    @model_validator(mode="after")
    def validate_answer(self) -> "QuestionResponseSubmission":
        if not self.answer.strip(): raise ValueError("answer must contain non-whitespace text")
        return self

class GradeAttemptRequest(BaseModel):
    responses: list[QuestionResponseSubmission] = Field(min_length=1, max_length=500)
    finalize: bool = True
    @model_validator(mode="after")
    def validate_response_ids(self) -> "GradeAttemptRequest":
        ids = [response.question_id for response in self.responses]
        if len(ids) != len(set(ids)): raise ValueError("each question may appear only once per request")
        return self

class FewShotGradeRequest(BaseModel):
    answer: str = Field(min_length=1)
    @model_validator(mode="after")
    def validate_answer(self) -> "FewShotGradeRequest":
        if not self.answer.strip(): raise ValueError("answer must contain non-whitespace text")
        return self

class AttemptGradeResponse(BaseModel):
    attempt: Attempt
    responses: list[Response]
    total_score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    percentage: float = Field(ge=0, le=100)
    completed_questions: int = Field(ge=0)
    total_questions: int = Field(ge=1)
