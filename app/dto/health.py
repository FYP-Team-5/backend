from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    postgres: Literal["ok"]
    qdrant: Literal["ok"]
    llm: Literal["ok"]
    model: str