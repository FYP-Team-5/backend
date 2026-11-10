from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel, field_validator

GradingOutputContract = Literal["criteria_array_v1", "grading_json_v1"]
BinaryCriterionLabel = Annotated[int, Field(strict=True, ge=0, le=1)]


class BinaryCriteriaResult(RootModel[list[BinaryCriterionLabel]]):
    """Grading LoRA output: one integer 0/1 per criterion, in rubric order."""

    root: list[BinaryCriterionLabel] = Field(min_length=1)


class CriteriaMetResult(BaseModel):
    criteria_id: str = Field(min_length=1)
    is_met: bool


class CriteriaEvaluationResult(BaseModel):
    """Legacy JSON-object grading output; score and feedback are not required."""

    criteria_met: list[CriteriaMetResult] = Field(min_length=1)


class FeedbackResult(BaseModel):
    """Validated plain-text feedback wrapped for internal use, not an LLM JSON contract."""

    feedback: str = Field(min_length=1)

    @field_validator("feedback")
    @classmethod
    def nonempty_feedback(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("feedback must contain non-whitespace text")
        return value


class CriteriaGradingResult(BaseModel):
    """Backend-assembled result after scoring labels and generating feedback.

    Never use this schema to parse the grading model's response.
    """

    score: float = Field(ge=0, allow_inf_nan=False)
    feedback: str = Field(min_length=1)
    criteria_met: list[CriteriaMetResult] = Field(default_factory=list)


class FewShotGradingResult(BaseModel):
    """Existing holistic few-shot contract, which still returns score and feedback."""

    score: float = Field(ge=0, allow_inf_nan=False)
    feedback: str = Field(min_length=1)
