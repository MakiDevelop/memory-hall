# ruff: noqa: B008

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from memory_hall.auth import Principal, get_principal
from memory_hall.models import (
    GetEntryResponse,
    LinkEntryRequest,
    LinkEntryResponse,
    ListEntriesResponse,
    LookupEntryResponse,
    PatchMemoryRequest,
    PatchMemoryResponse,
    SearchMemoryRequest,
    SearchMemoryResponse,
    WriteMemoryRequest,
    WriteMemoryResponse,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])
_audit_log = logging.getLogger("memory_hall.audit")


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
    principal: Principal = Depends(get_principal),
) -> SearchMemoryResponse:
    requested_agent_id = payload.agent_id

    if principal.is_privileged:
        decision = "allowed_privileged"
    else:
        # Bearer (user role): trust client-supplied agent_id, don't auto-scope.
        # Single-user deployment — Bearer token holder is trusted.
        # Cross-agent isolation enforced at token tier (HMAC vs Bearer), not query filter.
        decision = "allowed_user"

    runtime = request.app.state.runtime
    result = await runtime.search_entries(tenant_id=request.state.tenant_id, payload=payload)

    _audit_log.info(
        "search_privacy decision=%s principal=%s role=%s requested_agent_id=%s effective_agent_id=%s result_count=%d",
        decision,
        principal.principal_id,
        principal.role,
        requested_agent_id,
        payload.agent_id,
        result.total,
    )

    return result


@router.get("/by-hash", response_model=LookupEntryResponse)
async def get_by_content_hash(
    request: Request,
    content_hash: str = Query(min_length=1),
    namespace: str | None = Query(default=None),
) -> LookupEntryResponse:
    runtime = request.app.state.runtime
    entry = await runtime.get_entry_by_content_hash(
        tenant_id=request.state.tenant_id,
        content_hash=content_hash,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    if namespace is not None and entry.namespace != namespace:
        raise HTTPException(status_code=404, detail="entry not found")
    return LookupEntryResponse(entry=entry)


@router.get("/by-amh-hash", response_model=LookupEntryResponse)
async def get_by_amh_content_hash(
    request: Request,
    namespace: str = Query(min_length=1),
    hash: str = Query(min_length=1, alias="hash"),
) -> LookupEntryResponse:
    runtime = request.app.state.runtime
    entry = await runtime.get_entry_by_amh_content_hash(
        tenant_id=request.state.tenant_id,
        namespace=namespace,
        amh_content_hash=hash,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return LookupEntryResponse(entry=entry)


@router.get("/{entry_id}", response_model=GetEntryResponse)
async def get_entry(entry_id: str, request: Request) -> GetEntryResponse:
    runtime = request.app.state.runtime
    response = await runtime.get_entry(tenant_id=request.state.tenant_id, entry_id=entry_id)
    if response is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return response


@router.patch("/{entry_id}", response_model=PatchMemoryResponse)
async def patch_entry_metadata(
    entry_id: str,
    payload: PatchMemoryRequest,
    request: Request,
) -> PatchMemoryResponse:
    runtime = request.app.state.runtime
    entry = await runtime.patch_entry_metadata(
        tenant_id=request.state.tenant_id,
        entry_id=entry_id,
        metadata_patch=payload.metadata,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return PatchMemoryResponse(entry=entry)


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
    limit: int = Query(default=50, ge=1, le=1000),
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
