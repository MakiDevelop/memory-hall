# ruff: noqa: B008

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from memory_hall.auth import Principal, get_principal
from memory_hall.models import (
    GetEntryResponse,
    LinkEntryRequest,
    LinkEntryResponse,
    ListEntriesResponse,
    SearchMemoryRequest,
    SearchMemoryResponse,
    WriteMemoryRequest,
    WriteMemoryResponse,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])


@router.post("/write", response_model=WriteMemoryResponse)
async def write_entry(
    payload: WriteMemoryRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    runtime = request.app.state.runtime
    outcome = await runtime.write_entry(
        tenant_id=request.state.tenant_id,
        principal_id=principal.principal_id,
        payload=payload,
    )
    response = WriteMemoryResponse.from_outcome(outcome)
    return JSONResponse(status_code=outcome.status_code, content=response.model_dump(mode="json"))


@router.post("/search", response_model=SearchMemoryResponse)
async def search_entries(
    payload: SearchMemoryRequest,
    request: Request,
) -> SearchMemoryResponse:
    runtime = request.app.state.runtime
    return await runtime.search_entries(tenant_id=request.state.tenant_id, payload=payload)


@router.get("/{entry_id}", response_model=GetEntryResponse)
async def get_entry(entry_id: str, request: Request) -> GetEntryResponse:
    runtime = request.app.state.runtime
    response = await runtime.get_entry(tenant_id=request.state.tenant_id, entry_id=entry_id)
    if response is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return response


@router.post("/{entry_id}/link", response_model=LinkEntryResponse)
async def link_entry(
    entry_id: str,
    payload: LinkEntryRequest,
    request: Request,
) -> LinkEntryResponse:
    runtime = request.app.state.runtime
    entry = await runtime.link_entries(
        tenant_id=request.state.tenant_id,
        source_entry_id=entry_id,
        target_entry_id=payload.target_entry_id,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="source or target entry not found")
    return LinkEntryResponse(entry=entry)


@router.get("", response_model=ListEntriesResponse)
async def list_entries(
    request: Request,
    since: datetime | None = None,
    until: datetime | None = None,
    namespace: list[str] | None = Query(default=None),
    agent_id: str | None = None,
    type: list[str] | None = Query(default=None),
    tags: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> ListEntriesResponse:
    runtime = request.app.state.runtime
    return await runtime.list_entries(
        tenant_id=request.state.tenant_id,
        namespaces=namespace,
        agent_id=agent_id,
        types=type,
        tags=tags,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
