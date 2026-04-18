from __future__ import annotations

from fastapi import APIRouter, Request

from memory_hall.models import AuditResponse, ReindexResponse

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/reindex", response_model=ReindexResponse)
async def reindex(request: Request) -> ReindexResponse:
    runtime = request.app.state.runtime
    return await runtime.reindex(tenant_id=request.state.tenant_id)


@router.post("/audit", response_model=AuditResponse)
async def audit(request: Request) -> AuditResponse:
    runtime = request.app.state.runtime
    return await runtime.audit()
