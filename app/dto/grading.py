from pydantic import BaseModel, Field, field_validator


class CriteriaMetResult(BaseModel):
    criteria_id: str = Field(min_length=1)
    is_met: bool


class CriteriaEvaluationResult(BaseModel):
    criteria_met: list[CriteriaMetResult] = Field(min_length=1)


class FeedbackResult(BaseModel):
    feedback: str = Field(min_length=1)

    @field_validator("feedback")
    @classmethod
    def nonempty_feedback(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("feedback must contain non-whitespace text")
        return value


class CriteriaGradingResult(BaseModel):
    score: float = Field(ge=0)
    feedback: str = Field(min_length=1)
    criteria_met: list[CriteriaMetResult] = Field(default_factory=list)


class FewShotGradingResult(BaseModel):
    score: float = Field(ge=0)
    feedback: str = Field(min_length=1)
