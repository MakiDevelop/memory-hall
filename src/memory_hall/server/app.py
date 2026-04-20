from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from memory_hall.config import Settings
from memory_hall.embedder.http_embedder import HttpEmbedder
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
_BACKGROUND_REINDEX_INTERVAL_S = 120.0
_HEALTH_PROBE_INTERVAL_S = 30.0
_REINDEX_EMBED_BATCH_SIZE = 16

logger = logging.getLogger(__name__)


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
    pending_only: bool = False


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
        self._reindex_worker: asyncio.Task[None] | None = None
        self._health_probe_worker: asyncio.Task[None] | None = None
        self._background_reindex_interval_s = _BACKGROUND_REINDEX_INTERVAL_S
        self._background_reindex_jitter_s = min(15.0, _BACKGROUND_REINDEX_INTERVAL_S * 0.1)
        self._health_probe_interval_s = _HEALTH_PROBE_INTERVAL_S
        self._health_cache = HealthResponse(
            status="degraded",
            storage="degraded",
            vector_store="degraded",
            embedder="degraded",
        )

    async def start(self) -> None:
        self.settings.prepare_paths()
        await self.storage.open()
        self.vector_store.open()
        await self._refresh_health_cache()
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._consume_writes())
        self._reindex_worker = asyncio.create_task(self._run_background_reindex())
        self._health_probe_worker = asyncio.create_task(self._run_health_probe())

    async def stop(self) -> None:
        if self._health_probe_worker is not None:
            self._health_probe_worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._health_probe_worker
        if self._reindex_worker is not None:
            self._reindex_worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._reindex_worker
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
        return await self._queue_reindex(tenant_id=tenant_id, pending_only=False)

    async def _queue_reindex(
        self,
        *,
        tenant_id: str,
        pending_only: bool,
    ) -> ReindexResponse:
        future: asyncio.Future[ReindexResponse] = asyncio.get_running_loop().create_future()
        await self._require_queue().put(
            ReindexJob(
                tenant_id=tenant_id,
                future=future,
                pending_only=pending_only,
            )
        )
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
        return self._health_cache

    async def _refresh_health_cache(self) -> None:
        status = "ok"
        storage_status = "ok"
        vector_store_status = "ok"
        embedder_status = "ok"
        try:
            await self.storage.healthcheck()
        except Exception:
            status = "degraded"
            storage_status = "degraded"
        try:
            await asyncio.to_thread(self.vector_store.healthcheck)
        except Exception:
            status = "degraded"
            vector_store_status = "degraded"
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.embedder.embed, "healthcheck"),
                timeout=self.settings.health_embed_timeout_s,
            )
        except Exception:
            status = "degraded"
            embedder_status = "degraded"
        self._health_cache = HealthResponse(
            status=status,
            storage=storage_status,
            vector_store=vector_store_status,
            embedder=embedder_status,
        )

    async def _run_health_probe(self) -> None:
        while True:
            await asyncio.sleep(self._health_probe_interval_s)
            try:
                await self._refresh_health_cache()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("health probe failed: %s", exc)

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

    async def _run_background_reindex(self) -> None:
        tenant_id = self.settings.default_tenant_id
        while True:
            await asyncio.sleep(
                self._background_reindex_interval_s
                + random.uniform(0.0, self._background_reindex_jitter_s)  # noqa: S311
            )
            try:
                await self._queue_reindex(tenant_id=tenant_id, pending_only=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("background reindex failed: %s", exc)

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
        if job.pending_only:
            all_entries = await self.storage.list_pending_entries(
                job.tenant_id,
                limit=self.settings.reindex_batch_size,
            )
        else:
            all_entries = await self.storage.list_entries(job.tenant_id, limit=None)
        scanned = len(all_entries)
        candidates: list[Entry] = []
        embedded_count = 0
        pending_count = 0
        for entry in all_entries:
            if not job.pending_only:
                needs_reindex = entry.sync_status != SYNC_EMBEDDED
                if not needs_reindex:
                    needs_reindex = not await asyncio.to_thread(
                        self.vector_store.contains,
                        entry.tenant_id,
                        entry.entry_id,
                    )
                if not needs_reindex:
                    continue
            candidates.append(entry)
        for offset in range(0, len(candidates), _REINDEX_EMBED_BATCH_SIZE):
            embedded, pending = await self._embed_reindex_batch(
                candidates[offset : offset + _REINDEX_EMBED_BATCH_SIZE]
            )
            embedded_count += embedded
            pending_count += pending
        return ReindexResponse(scanned=scanned, embedded=embedded_count, pending=pending_count)

    async def _embed_reindex_batch(self, entries: list[Entry]) -> tuple[int, int]:
        if not entries:
            return (0, 0)
        try:
            vectors = await asyncio.wait_for(
                asyncio.to_thread(self.embedder.embed_batch, [entry.content for entry in entries]),
                timeout=self.settings.embed_timeout_s * len(entries),
            )
            if len(vectors) != len(entries):
                raise ValueError("embed_batch returned mismatched vector count")
        except Exception:
            embedded_count = 0
            pending_count = 0
            for entry in entries:
                try:
                    embedded = await self._embed_reindex_entry(entry)
                    embedded_count += int(embedded)
                except Exception:
                    pending_count += 1
            return (embedded_count, pending_count)
        embedded_count = 0
        pending_count = 0
        for entry, vector in zip(entries, vectors, strict=True):
            try:
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
        return (embedded_count, pending_count)

    async def _embed_reindex_entry(self, entry: Entry) -> bool:
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
        return True

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
    embed_dim = active_settings.embed_dim or active_settings.vector_dim
    if embedder is None:
        if active_settings.embedder_kind == "http":
            if not active_settings.embed_base_url:
                raise ValueError("embed_base_url is required when embedder_kind='http'")
            active_embedder = HttpEmbedder(
                base_url=active_settings.embed_base_url,
                timeout_s=max(active_settings.embed_timeout_s, 10.0),
                dim=embed_dim,
            )
        else:
            active_embedder = OllamaEmbedder(
                base_url=active_settings.ollama_base_url,
                model=active_settings.ollama_model,
                timeout_s=active_settings.embed_timeout_s,
                dim=embed_dim,
            )
    else:
        active_embedder = embedder
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

    @app.middleware("http")
    async def enforce_write_content_limit(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/v1/memory/write":
            body = await request.body()
            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    content = payload.get("content")
                    if isinstance(content, str):
                        if len(content.encode("utf-8")) > active_settings.max_content_bytes:
                            return JSONResponse(
                                status_code=413,
                                content={
                                    "detail": (
                                        f"content exceeds {active_settings.max_content_bytes} bytes"
                                    )
                                },
                            )

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request(request.scope, receive)
        return await call_next(request)

    app.add_middleware(TenantMiddleware, tenant_id=active_settings.default_tenant_id)
    app.include_router(health_router)
    app.include_router(memory_router)
    app.include_router(admin_router)
    return app
