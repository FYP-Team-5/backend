from typing import Any

from pydantic import BaseModel, Field, model_validator


class RetrievedRubricChunk(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

class CriterionGrade(BaseModel):
    criterion: str = Field(min_length=1)
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    feedback: str = Field(min_length=1)
    @model_validator(mode="after")
    def validate_score(self) -> "CriterionGrade":
        if self.score > self.max_score: raise ValueError("criterion score cannot exceed max_score")
        return self

class GradingResult(BaseModel):
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    feedback: str = Field(min_length=1)
    criteria: list[CriterionGrade] = Field(default_factory=list)
    @model_validator(mode="after")
    def validate_score(self) -> "GradingResult":
        if self.score > self.max_score: raise ValueError("score cannot exceed max_score")
        return self
