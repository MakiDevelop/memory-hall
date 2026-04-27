from __future__ import annotations

import sqlite3

import pytest

from memory_hall.models import SYNC_FAILED, SYNC_PENDING, WriteMemoryRequest
from memory_hall.server.app import build_runtime
from memory_hall.storage.sqlite_store import SqliteStore
from tests.conftest import TimeoutEmbedder, build_settings


@pytest.mark.asyncio
async def test_reindex_failures_increment_retry_and_stop_picking_failed(tmp_path) -> None:
    settings = build_settings(tmp_path)
    runtime = build_runtime(
        settings=settings,
        embedder=TimeoutEmbedder(dim=settings.vector_dim),
    )
    await runtime.start()
    try:
        outcome = await runtime.write_entry(
            tenant_id=settings.default_tenant_id,
            principal_id="pytest",
            payload=WriteMemoryRequest(
                agent_id="codex",
                namespace="shared",
                type="note",
                content="poison pill pending entry",
            ),
        )
        assert outcome.status_code == 202
        assert outcome.entry.sync_status == SYNC_PENDING
        assert outcome.entry.embed_attempt_count == 1
        assert outcome.entry.last_embed_attempted_at is not None
        assert outcome.entry.last_embed_error is not None
        assert outcome.entry.last_embed_error.startswith("TimeoutError")

        for expected_attempt_count in range(2, 6):
            pending_entries = await runtime.storage.list_pending_entries(settings.default_tenant_id)
            assert [entry.entry_id for entry in pending_entries] == [outcome.entry.entry_id]

            embedded, pending = await runtime._embed_reindex_batch(pending_entries)
            assert embedded == 0
            assert pending == 1

            stored_entry = await runtime.storage.get_entry(
                settings.default_tenant_id,
                outcome.entry.entry_id,
            )
            assert stored_entry is not None
            assert stored_entry.embed_attempt_count == expected_attempt_count

        final_entry = await runtime.storage.get_entry(
            settings.default_tenant_id,
            outcome.entry.entry_id,
        )
        assert final_entry is not None
        assert final_entry.sync_status == SYNC_FAILED
        assert final_entry.embed_attempt_count == 5
        assert await runtime.storage.list_pending_entries(settings.default_tenant_id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_store_migrates_legacy_entries_without_data_loss(tmp_path) -> None:
    settings = build_settings(tmp_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE entries (
                entry_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                summary TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                references_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                sync_status TEXT NOT NULL DEFAULT 'pending',
                last_embedded_at TEXT,
                created_at TEXT NOT NULL,
                created_by_principal TEXT NOT NULL,
                UNIQUE (tenant_id, content_hash)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO entries (
                entry_id, tenant_id, agent_id, namespace, type, content, content_hash,
                summary, tags_json, references_json, metadata_json, sync_status,
                last_embedded_at, created_at, created_by_principal
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "01KPREINDEXRETRYTEST0000001",
                settings.default_tenant_id,
                "pytest",
                "shared",
                "note",
                "legacy pending row",
                "sha256:legacy",
                None,
                "[]",
                "[]",
                "{}",
                SYNC_PENDING,
                None,
                "2026-04-27T00:00:00+00:00",
                "pytest",
            ),
        )
        connection.commit()

    store = SqliteStore(settings.database_path)
    await store.open()
    try:
        entry = await store.get_entry(settings.default_tenant_id, "01KPREINDEXRETRYTEST0000001")
        assert entry is not None
        assert entry.content == "legacy pending row"
        assert entry.last_embed_error is None
        assert entry.last_embed_attempted_at is None
        assert entry.embed_attempt_count == 0
    finally:
        await store.close()
