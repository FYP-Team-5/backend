from pydantic import BaseModel, Field


class CriteriaMetResult(BaseModel):
    criteria_id: str = Field(min_length=1)
    is_met: bool


class CriteriaGradingResult(BaseModel):
    score: float = Field(ge=0)
    feedback: str = Field(min_length=1)
    criteria_met: list[CriteriaMetResult] = Field(default_factory=list)


class FewShotGradingResult(BaseModel):
    score: float = Field(ge=0)
    feedback: str = Field(min_length=1)
