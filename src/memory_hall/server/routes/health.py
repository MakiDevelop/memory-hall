from __future__ import annotations

from fastapi import APIRouter, Request

from memory_hall.models import HealthResponse

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    runtime = request.app.state.runtime
    return await runtime.health()
