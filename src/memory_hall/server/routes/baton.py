# ruff: noqa: B008

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from memory_hall.auth import Principal, get_principal
from memory_hall.models import (
    BatonReadRequest,
    BatonReadResponse,
    BatonWriteRequest,
    BatonWriteResponse,
)

router = APIRouter(prefix="/v1/baton", tags=["baton"])


@router.post("/read", response_model=BatonReadResponse)
async def read_baton(
    payload: BatonReadRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> BatonReadResponse:
    runtime = request.app.state.runtime
    baton, updated_at = await runtime.storage.baton_read(payload.namespace)
    return BatonReadResponse(baton=baton, namespace=payload.namespace, updated_at=updated_at)


@router.post("/write", response_model=BatonWriteResponse)
async def write_baton(
    payload: BatonWriteRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> BatonWriteResponse:
    runtime = request.app.state.runtime
    updated_at = await runtime.storage.baton_write(payload.namespace, payload.baton)
    return BatonWriteResponse(ok=True, updated_at=updated_at, namespace=payload.namespace)
