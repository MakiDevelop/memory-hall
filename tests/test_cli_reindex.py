from __future__ import annotations

from datetime import timedelta

import pytest

from memory_hall.cli.main import _reindex_fts
from memory_hall.models import Entry, build_content_hash, utc_now
from memory_hall.storage.sqlite_store import SqliteStore


def _entry(index: int) -> Entry:
    content = f"entry {index}"
    return Entry(
        entry_id=f"01KPGCLIINDEX{index:08d}",
        tenant_id="default",
        agent_id="pytest",
        namespace="shared",
        type="note",
        content=content,
        content_hash=build_content_hash(content),
        summary=None,
        tags=[],
        references=[],
        metadata={},
        sync_status="pending",
        last_embedded_at=None,
        last_embed_error=None,
        last_embed_attempted_at=None,
        embed_attempt_count=0,
        created_at=utc_now() - timedelta(seconds=index),
        created_by_principal="pytest",
    )


@pytest.mark.asyncio
async def test_reindex_fts_streams_batches(monkeypatch, tmp_path) -> None:
    batches = [
        [_entry(index) for index in range(500)],
        [_entry(index) for index in range(500, 1000)],
        [_entry(index) for index in range(1000, 1500)],
        [_entry(index) for index in range(1500, 2000)],
    ]
    list_calls: list[tuple[int | None, str | None]] = []
    reindex_calls: list[int] = []

    async def fake_open(self) -> None:
        del self

    async def fake_close(self) -> None:
        del self

    async def fake_list_entries(self, tenant_id: str, **kwargs):
        del self, tenant_id
        list_calls.append((kwargs.get("limit"), kwargs.get("cursor")))
        if not batches:
            return []
        return batches.pop(0)

    async def fake_reindex_fts_entries(self, entries: list[Entry]) -> int:
        del self
        reindex_calls.append(len(entries))
        return len(entries)

    monkeypatch.setattr(SqliteStore, "open", fake_open)
    monkeypatch.setattr(SqliteStore, "close", fake_close)
    monkeypatch.setattr(SqliteStore, "list_entries", fake_list_entries)
    monkeypatch.setattr(SqliteStore, "reindex_fts_entries", fake_reindex_fts_entries)

    await _reindex_fts(
        tenant_id="default",
        batch_size=500,
        database_path=tmp_path / "memory-hall.sqlite3",
    )

    assert [call[0] for call in list_calls] == [500, 500, 500, 500, 500]
    assert list_calls[0][1] is None
    assert all(call[1] is not None for call in list_calls[1:])
    assert reindex_calls == [500, 500, 500, 500]
