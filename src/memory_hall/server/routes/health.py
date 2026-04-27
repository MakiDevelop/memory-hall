from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from memory_hall.models import HealthResponse

router = APIRouter(prefix="/v1", tags=["health"])


def _ready_response(response: HealthResponse) -> JSONResponse:
    status_code = 200 if response.status == "ok" else 503
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    return JSONResponse(status_code=200, content=await runtime.healthz())


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    return _ready_response(await runtime.ready())


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    return _ready_response(await runtime.health())
