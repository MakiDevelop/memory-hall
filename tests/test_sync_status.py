from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from memory_hall.models import SYNC_FAILED, SYNC_PENDING, Entry, build_content_hash, utc_now
from memory_hall.server.app import ReindexJob, build_runtime
from tests.conftest import DeterministicEmbedder, TimeoutEmbedder, build_settings, client_for_app


class BatchTrackingEmbedder(DeterministicEmbedder):
    def __init__(self, dim: int = 8) -> None:
        super().__init__(dim=dim)
        self.embed_calls = 0
        self.embed_batch_calls: list[int] = []

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return super().embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.embed_batch_calls.append(len(texts))
        return [DeterministicEmbedder.embed(self, text) for text in texts]


def _entry(index: int, *, tenant_id: str, sync_status: str) -> Entry:
    content = f"{sync_status} backlog entry {index}"
    return Entry(
        entry_id=f"01KPHA5REINDEX{index:012d}",
        tenant_id=tenant_id,
        agent_id="pytest",
        namespace="shared",
        type="note",
        content=content,
        content_hash=build_content_hash(f"{content}-{index}"),
        summary=None,
        tags=[],
        references=[],
        metadata={},
        sync_status=sync_status,
        last_embedded_at=None,
        last_embed_error=None,
        last_embed_attempted_at=None,
        embed_attempt_count=0,
        created_at=utc_now() - timedelta(seconds=index),
        created_by_principal="pytest",
    )


@pytest.mark.asyncio
async def test_pending_write_reindexes_to_embedded(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "pending entry waiting for embedder",
            },
        )
        assert write_response.status_code == 202
        payload = write_response.json()
        assert payload["sync_status"] == "pending"

        app.state.runtime.embedder = DeterministicEmbedder(dim=app.state.settings.vector_dim)
        reindex_response = await client.post("/v1/admin/reindex")
        assert reindex_response.status_code == 200
        assert reindex_response.json()["embedded"] >= 1

        get_response = await client.get(f"/v1/memory/{payload['entry_id']}")
        assert get_response.status_code == 200
        assert get_response.json()["entry"]["sync_status"] == "embedded"


@pytest.mark.asyncio
async def test_reindex_uses_embed_batch_for_pending_backlog(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        for index in range(3):
            response = await client.post(
                "/v1/memory/write",
                json={
                    "agent_id": "codex",
                    "namespace": "shared",
                    "type": "note",
                    "content": f"pending batch entry {index}",
                },
            )
            assert response.status_code == 202

        tracking = BatchTrackingEmbedder(dim=app.state.settings.vector_dim)
        app.state.runtime.embedder = tracking

        reindex_response = await client.post("/v1/admin/reindex")
        assert reindex_response.status_code == 200
        assert reindex_response.json()["embedded"] == 3
        assert tracking.embed_batch_calls == [3]
        assert tracking.embed_calls == 0


@pytest.mark.asyncio
async def test_admin_reindex_paginates_failed_backlog(app_factory, monkeypatch, caplog) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        runtime = app.state.runtime
        tenant_id = app.state.settings.default_tenant_id
        for index in range(205):
            await runtime.storage.insert_entry(
                _entry(index, tenant_id=tenant_id, sync_status=SYNC_FAILED)
            )

        original_list_entries = runtime.storage.list_entries
        list_calls: list[dict[str, object]] = []

        async def tracked_list_entries(tenant: str, **kwargs):
            assert tenant == tenant_id
            assert kwargs.get("limit") == 200
            list_calls.append(dict(kwargs))
            return await original_list_entries(tenant, **kwargs)

        monkeypatch.setattr(runtime.storage, "list_entries", tracked_list_entries)

        with caplog.at_level(logging.INFO):
            response = await client.post("/v1/admin/reindex")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scanned"] == 205
    assert payload["embedded"] == 205
    assert payload["pending"] == 0
    assert len(list_calls) == 2
    assert [call["cursor"] for call in list_calls] == [None, list_calls[1]["cursor"]]
    assert list_calls[1]["cursor"] is not None
    assert all(call["sync_status"] is None for call in list_calls)
    assert any("reindex batch 1/2, 200 done" in record.message for record in caplog.records)
    assert any("reindex batch 2/2, 205 done" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_pending_only_reindex_paginates_pending_entries_only(tmp_path, monkeypatch) -> None:
    settings = build_settings(tmp_path)
    runtime = build_runtime(
        settings=settings,
        embedder=DeterministicEmbedder(dim=settings.vector_dim),
    )
    await runtime.start()
    try:
        for index in range(205):
            await runtime.storage.insert_entry(
                _entry(index, tenant_id=settings.default_tenant_id, sync_status=SYNC_PENDING)
            )
        failed_entry = _entry(9999, tenant_id=settings.default_tenant_id, sync_status=SYNC_FAILED)
        await runtime.storage.insert_entry(failed_entry)

        original_list_entries = runtime.storage.list_entries
        list_calls: list[dict[str, object]] = []

        async def tracked_list_entries(tenant: str, **kwargs):
            assert tenant == settings.default_tenant_id
            assert kwargs.get("limit") == 200
            assert kwargs.get("sync_status") == SYNC_PENDING
            list_calls.append(dict(kwargs))
            return await original_list_entries(tenant, **kwargs)

        monkeypatch.setattr(runtime.storage, "list_entries", tracked_list_entries)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        outcome = await runtime._handle_reindex(
            ReindexJob(
                tenant_id=settings.default_tenant_id,
                future=future,
                pending_only=True,
            )
        )
    finally:
        await runtime.stop()

    assert outcome.scanned == 205
    assert outcome.embedded == 205
    assert outcome.pending == 0
    assert len(list_calls) == 2
    assert [call["cursor"] for call in list_calls] == [None, list_calls[1]["cursor"]]
    assert list_calls[1]["cursor"] is not None
