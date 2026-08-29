from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.controller.dependencies import get_identity_service
from app.dto import (
    HealthResponse,
)
from app.service import (
    IdentityService,
)

health_router = APIRouter(tags=["health"])

@health_router.get("/health", response_model=HealthResponse)
async def health(
    service: Annotated[IdentityService, Depends(get_identity_service)],
) -> HealthResponse:
    components = await service.health()
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
        qdrant="ok",
        llm="ok",
        model=service.settings.llm_model,
    )