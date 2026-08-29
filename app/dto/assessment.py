from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.model.assessment import Attempt, QuestionGrade

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"

class CourseCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)

class QuestionCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=10_000)
    max_score: float = Field(gt=0)
    rubric_chunk_indexes: list[int] = Field(default_factory=list)
    @field_validator("rubric_chunk_indexes")
    @classmethod
    def validate_indexes(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value): raise ValueError("rubric chunk indexes must be non-negative")
        if len(value) != len(set(value)): raise ValueError("rubric chunk indexes must be unique")
        return value

class ExamCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    type: Literal["exam", "quiz"] = "exam"
    max_attempts: int = Field(default=1, ge=1, le=100)
    rubric_id: str = Field(pattern=ID_PATTERN)
    questions: list[QuestionCreate] = Field(min_length=1, max_length=500)
    @model_validator(mode="after")
    def validate_question_ids(self) -> "ExamCreate":
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)): raise ValueError("question ids must be unique within an exam")
        return self

class ExamRubricUpdate(BaseModel):
    rubric_id: str = Field(pattern=ID_PATTERN)

class RubricChunkMappingRequest(BaseModel):
    chunk_indexes: list[int] = Field(min_length=1)
    @field_validator("chunk_indexes")
    @classmethod
    def validate_indexes(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value): raise ValueError("chunk indexes must be non-negative")
        if len(value) != len(set(value)): raise ValueError("chunk indexes must be unique")
        return value

class QuestionResponseSubmission(BaseModel):
    question_id: str = Field(pattern=ID_PATTERN)
    answer: str = Field(min_length=1)
    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str) -> str:
        if not value.strip(): raise ValueError("answer must contain non-whitespace text")
        return value

class GradeAttemptRequest(BaseModel):
    responses: list[QuestionResponseSubmission] = Field(min_length=1, max_length=500)
    finalize: bool = True
    @model_validator(mode="after")
    def validate_response_ids(self) -> "GradeAttemptRequest":
        ids = [response.question_id for response in self.responses]
        if len(ids) != len(set(ids)): raise ValueError("each question may appear only once per request")
        return self

class AttemptGradeResponse(BaseModel):
    attempt: Attempt
    grades: list[QuestionGrade]
    total_score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    percentage: float = Field(ge=0, le=100)
    completed_questions: int = Field(ge=0)
    total_questions: int = Field(ge=1)
