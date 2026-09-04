from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.controller.dependencies import get_grading_service, get_identity_service
from app.dto import (
    HealthResponse,
)
from app.service import (
    GradingService,
    IdentityService,
)

health_router = APIRouter(tags=["health"])

@health_router.get("/health", response_model=HealthResponse)
async def health(
    identity_service: Annotated[IdentityService, Depends(get_identity_service)],
    grading_service: Annotated[GradingService, Depends(get_grading_service)],
) -> HealthResponse:
    components = await grading_service.health()
    components["postgres"] = components["postgres"] and await identity_service.health()
    if not all(components.values()):
        raise HTTPException(
            status_code=503,
            detail={
                name: "ok" if available else "unavailable"
                for name, available in components.items()
            },
        )
    return HealthResponse(
        status="ok",
        postgres="ok",
        llm="ok",
        model=grading_service.settings.llm_model,
    )