# ruff: noqa: B008

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from memory_hall.auth import Principal, get_principal
from memory_hall.models import (
    BatonReadRequest,
    BatonReadResponse,
    BatonWriteRequest,
    BatonWriteResponse,
)
from memory_hall.storage.sqlite_store import BatonCasConflictError

router = APIRouter(prefix="/v1/baton", tags=["baton"])


@router.post("/read", response_model=BatonReadResponse)
async def read_baton(
    payload: BatonReadRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> BatonReadResponse:
    runtime = request.app.state.runtime
    baton, updated_at, revision = await runtime.storage.baton_read(
        request.state.tenant_id,
        payload.namespace,
    )
    return BatonReadResponse(
        baton=baton,
        namespace=payload.namespace,
        updated_at=updated_at,
        revision=revision,
    )


@router.post("/write", response_model=BatonWriteResponse)
async def write_baton(
    payload: BatonWriteRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> BatonWriteResponse:
    runtime = request.app.state.runtime
    try:
        updated_at, revision = await runtime.storage.baton_write(
            request.state.tenant_id,
            payload.namespace,
            payload.baton,
            expected_revision=payload.expected_revision,
        )
    except BatonCasConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "baton_revision_conflict",
                "namespace": exc.namespace,
                "expected_revision": exc.expected_revision,
                "current_baton": exc.current_baton,
                "current_updated_at": exc.current_updated_at,
                "current_revision": exc.current_revision,
            },
        ) from exc
    return BatonWriteResponse(
        ok=True,
        updated_at=updated_at,
        namespace=payload.namespace,
        revision=revision,
    )
