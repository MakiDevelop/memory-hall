from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from memory_hall.models import HealthResponse

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    response = await runtime.health()
    status_code = 200 if response.status == "ok" else 503
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))
