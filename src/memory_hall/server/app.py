from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from memory_hall.config import Settings
from memory_hall.embedder.interface import Embedder
from memory_hall.embedder.ollama_embedder import OllamaEmbedder
from memory_hall.models import (
    SYNC_EMBEDDED,
    SYNC_PENDING,
    AuditResponse,
    Entry,
    EntryDocument,
    GetEntryResponse,
    HealthResponse,
    ListEntriesResponse,
    ReindexResponse,
    ScoreBreakdown,
    SearchMemoryRequest,
    SearchMemoryResponse,
    SearchResultItem,
    WriteMemoryRequest,
    WriteOutcome,
    build_content_hash,
    encode_cursor,
    new_entry_id,
    utc_now,
)
from memory_hall.server.middleware.tenant import TenantMiddleware
from memory_hall.server.routes import admin_router, health_router, memory_router
from memory_hall.storage.interface import Storage
from memory_hall.storage.sqlite_store import SqliteStore
from memory_hall.storage.vector_store import SqliteVecStore, VectorStore

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_RRF_K = 60


@dataclass(slots=True)
class WriteJob:
    tenant_id: str
    principal_id: str
    payload: WriteMemoryRequest
    future: asyncio.Future[WriteOutcome]


@dataclass(slots=True)
class LinkJob:
    tenant_id: str
    source_entry_id: str
    target_entry_id: str
    future: asyncio.Future[EntryDocument | None]


@dataclass(slots=True)
class ReindexJob:
    tenant_id: str
    future: asyncio.Future[ReindexResponse]


class MemoryHallRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        vector_store: VectorStore,
        embedder: Embedder,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.vector_store = vector_store
        self.embedder = embedder
        self._queue: asyncio.Queue[WriteJob | LinkJob | ReindexJob | None] | None = None
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.settings.prepare_paths()
        await self.storage.open()
        self.vector_store.open()
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._consume_writes())

    async def stop(self) -> None:
        if self._queue is not None:
            await self._queue.put(None)
        if self._worker is not None:
            await self._worker
        self.vector_store.close()
        await self.storage.close()

    async def write_entry(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        payload: WriteMemoryRequest,
    ) -> WriteOutcome:
        future: asyncio.Future[WriteOutcome] = asyncio.get_running_loop().create_future()
        await self._require_queue().put(
            WriteJob(
                tenant_id=tenant_id,
                principal_id=principal_id,
                payload=payload,
                future=future,
            )
        )
        return await future

    async def link_entries(
        self,
        *,
        tenant_id: str,
        source_entry_id: str,
        target_entry_id: str,
    ) -> EntryDocument | None:
        future: asyncio.Future[EntryDocument | None] = asyncio.get_running_loop().create_future()
        await self._require_queue().put(
            LinkJob(
                tenant_id=tenant_id,
                source_entry_id=source_entry_id,
                target_entry_id=target_entry_id,
                future=future,
            )
        )
        return await future

    async def reindex(self, *, tenant_id: str) -> ReindexResponse:
        future: asyncio.Future[ReindexResponse] = asyncio.get_running_loop().create_future()
        await self._require_queue().put(ReindexJob(tenant_id=tenant_id, future=future))
        return await future

    async def search_entries(
        self,
        *,
        tenant_id: str,
        payload: SearchMemoryRequest,
    ) -> SearchMemoryResponse:
        limit = payload.limit or self.settings.search_default_limit
        candidate_limit = max(limit, limit * self.settings.search_candidate_multiplier)
        lexical_hits: list[tuple[str, float]] = []
        semantic_hits: list[tuple[str, float]] = []

        if payload.mode in {"lexical", "hybrid"}:
            lexical_candidates = await self.storage.search_lexical(
                tenant_id=tenant_id,
                query=payload.query,
                namespaces=payload.namespace,
                agent_id=payload.agent_id,
                types=payload.type,
                tags=payload.tags,
                limit=candidate_limit,
            )
            lexical_hits = [
                (candidate.entry_id, candidate.score) for candidate in lexical_candidates
            ]

        if payload.mode in {"semantic", "hybrid"}:
            try:
                query_vector = await asyncio.wait_for(
                    asyncio.to_thread(self.embedder.embed, payload.query),
                    timeout=self.settings.embed_timeout_s,
                )
                semantic_candidates = await asyncio.to_thread(
                    self.vector_store.search,
                    tenant_id,
                    query_vector,
                    candidate_limit,
                )
                semantic_hits = [
                    (candidate.entry_id, candidate.score) for candidate in semantic_candidates
                ]
            except Exception:
                if payload.mode == "semantic":
                    semantic_hits = []

        combined = self._combine_hits(payload.query, lexical_hits, semantic_hits, limit)
        entry_ids = [item["entry_id"] for item in combined]
        entries = await self.storage.get_entries_by_ids(tenant_id, entry_ids)
        entry_map = {entry.entry_id: entry for entry in entries}
        results: list[SearchResultItem] = []
        for item in combined:
            entry = entry_map.get(item["entry_id"])
            if entry is None:
                continue
            results.append(
                SearchResultItem(
                    entry_id=entry.entry_id,
                    score=item["rrf"],
                    score_breakdown=ScoreBreakdown(
                        bm25=item["bm25"],
                        semantic=item["semantic"],
                        rrf=item["rrf"],
                    ),
                    entry=EntryDocument.from_entry(entry),
                )
            )
        return SearchMemoryResponse(results=results, total=len(results))

    async def get_entry(self, *, tenant_id: str, entry_id: str) -> GetEntryResponse | None:
        entry = await self.storage.get_entry(tenant_id, entry_id)
        if entry is None:
            return None
        references_out = await self.storage.get_references_out(tenant_id, entry_id)
        references_in = await self.storage.get_references_in(tenant_id, entry_id)
        return GetEntryResponse(
            entry=EntryDocument.from_entry(entry),
            references_out=[EntryDocument.from_entry(item) for item in references_out],
            references_in=[EntryDocument.from_entry(item) for item in references_in],
        )

    async def list_entries(
        self,
        *,
        tenant_id: str,
        namespaces: list[str] | None,
        agent_id: str | None,
        types: list[str] | None,
        tags: list[str] | None,
        since,
        until,
        limit: int,
        cursor: str | None,
    ) -> ListEntriesResponse:
        entries = await self.storage.list_entries(
            tenant_id,
            namespaces=namespaces,
            agent_id=agent_id,
            types=types,
            tags=tags,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )
        next_cursor = None
        if entries and len(entries) == limit:
            tail = entries[-1]
            next_cursor = encode_cursor(tail.created_at, tail.entry_id)
        return ListEntriesResponse(
            entries=[EntryDocument.from_entry(entry) for entry in entries],
            next_cursor=next_cursor,
        )

    async def health(self) -> HealthResponse:
        await self.storage.healthcheck()
        await asyncio.to_thread(self.vector_store.healthcheck)
        return HealthResponse(status="ok", storage="ok", vector_store="ok", embedder="ok")

    async def audit(self) -> AuditResponse:
        payload = await self.storage.audit()
        return AuditResponse.model_validate(payload)

    async def _consume_writes(self) -> None:
        queue = self._require_queue()
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                break
            try:
                if isinstance(job, WriteJob):
                    job.future.set_result(await self._handle_write(job))
                elif isinstance(job, LinkJob):
                    job.future.set_result(await self._handle_link(job))
                elif isinstance(job, ReindexJob):
                    job.future.set_result(await self._handle_reindex(job))
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                queue.task_done()

    async def _handle_write(self, job: WriteJob) -> WriteOutcome:
        created_at = utc_now()
        entry = Entry(
            entry_id=new_entry_id(),
            tenant_id=job.tenant_id,
            agent_id=job.payload.agent_id,
            namespace=job.payload.namespace,
            type=job.payload.type,
            content=job.payload.content,
            content_hash=build_content_hash(job.payload.content),
            summary=job.payload.summary,
            tags=job.payload.tags,
            references=job.payload.references,
            metadata=job.payload.metadata,
            sync_status=SYNC_PENDING,
            last_embedded_at=None,
            created_at=created_at,
            created_by_principal=job.principal_id,
        )
        outcome = await self.storage.insert_entry(entry)
        if not outcome.created:
            return WriteOutcome(
                entry=outcome.entry,
                created=False,
                embedded=outcome.entry.sync_status == SYNC_EMBEDDED,
                status_code=200,
            )
        try:
            vector = await asyncio.wait_for(
                asyncio.to_thread(self.embedder.embed, outcome.entry.content),
                timeout=self.settings.embed_timeout_s,
            )
            await asyncio.to_thread(
                self.vector_store.upsert,
                outcome.entry.tenant_id,
                outcome.entry.entry_id,
                vector,
            )
            embedded_at = utc_now()
            embedded_entry = await self.storage.update_sync_status(
                outcome.entry.tenant_id,
                outcome.entry.entry_id,
                SYNC_EMBEDDED,
                embedded_at,
            )
            if embedded_entry is None:
                embedded_entry = outcome.entry
            return WriteOutcome(
                entry=embedded_entry,
                created=True,
                embedded=True,
                status_code=201,
            )
        except Exception:
            pending_entry = await self.storage.get_entry(
                outcome.entry.tenant_id,
                outcome.entry.entry_id,
            )
            return WriteOutcome(
                entry=pending_entry or outcome.entry,
                created=True,
                embedded=False,
                status_code=202,
            )

    async def _handle_link(self, job: LinkJob) -> EntryDocument | None:
        entry = await self.storage.add_reference(
            tenant_id=job.tenant_id,
            source_entry_id=job.source_entry_id,
            target_entry_id=job.target_entry_id,
        )
        if entry is None:
            return None
        return EntryDocument.from_entry(entry)

    async def _handle_reindex(self, job: ReindexJob) -> ReindexResponse:
        all_entries = await self.storage.list_entries(job.tenant_id, limit=None)
        scanned = len(all_entries)
        embedded_count = 0
        pending_count = 0
        for entry in all_entries:
            needs_reindex = entry.sync_status != SYNC_EMBEDDED
            if not needs_reindex:
                needs_reindex = not await asyncio.to_thread(
                    self.vector_store.contains,
                    entry.tenant_id,
                    entry.entry_id,
                )
            if not needs_reindex:
                continue
            try:
                vector = await asyncio.wait_for(
                    asyncio.to_thread(self.embedder.embed, entry.content),
                    timeout=self.settings.embed_timeout_s,
                )
                await asyncio.to_thread(
                    self.vector_store.upsert,
                    entry.tenant_id,
                    entry.entry_id,
                    vector,
                )
                await self.storage.update_sync_status(
                    entry.tenant_id,
                    entry.entry_id,
                    SYNC_EMBEDDED,
                    utc_now(),
                )
                embedded_count += 1
            except Exception:
                pending_count += 1
        return ReindexResponse(scanned=scanned, embedded=embedded_count, pending=pending_count)

    def _require_queue(self) -> asyncio.Queue[WriteJob | LinkJob | ReindexJob | None]:
        if self._queue is None:
            raise RuntimeError("runtime is not started")
        return self._queue

    @staticmethod
    def _combine_hits(
        query: str,
        lexical_hits: list[tuple[str, float]],
        semantic_hits: list[tuple[str, float]],
        limit: int,
    ) -> list[dict[str, Any]]:
        lexical_weight = 2.0 if len(query) < 5 and _CJK_RE.search(query) else 1.0
        combined: dict[str, dict[str, Any]] = {}
        for rank, (entry_id, score) in enumerate(lexical_hits, start=1):
            payload = combined.setdefault(
                entry_id,
                {"entry_id": entry_id, "bm25": 0.0, "semantic": 0.0, "rrf": 0.0},
            )
            payload["bm25"] = score
            payload["rrf"] += lexical_weight / (_RRF_K + rank)
        for rank, (entry_id, score) in enumerate(semantic_hits, start=1):
            payload = combined.setdefault(
                entry_id,
                {"entry_id": entry_id, "bm25": 0.0, "semantic": 0.0, "rrf": 0.0},
            )
            payload["semantic"] = score
            payload["rrf"] += 1.0 / (_RRF_K + rank)
        ranked = sorted(combined.values(), key=lambda item: item["rrf"], reverse=True)
        return ranked[:limit]


def build_runtime(
    *,
    settings: Settings | None = None,
    storage: Storage | None = None,
    vector_store: VectorStore | None = None,
    embedder: Embedder | None = None,
) -> MemoryHallRuntime:
    """Assemble a MemoryHallRuntime with default components from settings.

    For HTTP use, prefer `create_app` (it builds and manages the runtime via lifespan).
    For embedded / in-process use (e.g. sandboxed agents that cannot open sockets),
    call `build_runtime()` then `await runtime.start()` / `await runtime.stop()` yourself.
    """
    active_settings = settings or Settings()
    active_storage = storage or SqliteStore(active_settings.database_path)
    active_vector_store = vector_store or SqliteVecStore(
        active_settings.vector_database_path,
        dim=active_settings.vector_dim,
    )
    active_embedder = embedder or OllamaEmbedder(
        base_url=active_settings.ollama_base_url,
        model=active_settings.ollama_model,
        timeout_s=active_settings.embed_timeout_s,
        dim=active_settings.vector_dim,
    )
    return MemoryHallRuntime(
        settings=active_settings,
        storage=active_storage,
        vector_store=active_vector_store,
        embedder=active_embedder,
    )


def create_app(
    *,
    settings: Settings | None = None,
    storage: Storage | None = None,
    vector_store: VectorStore | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    runtime = build_runtime(
        settings=settings,
        storage=storage,
        vector_store=vector_store,
        embedder=embedder,
    )
    active_settings = runtime.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        app.state.runtime = runtime
        app.state.settings = active_settings
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="memory-hall", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TenantMiddleware, tenant_id=active_settings.default_tenant_id)
    app.include_router(health_router)
    app.include_router(memory_router)
    app.include_router(admin_router)
    return app
