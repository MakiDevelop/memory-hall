from __future__ import annotations

import sqlite3

import pytest

from memory_hall.models import Entry, build_content_hash, utc_now
from memory_hall.storage.sqlite_store import SqliteStore
from tests.conftest import build_settings, client_for_app


@pytest.mark.asyncio
async def test_cjk_write_search_hits_lexical(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "最近又撞牆，需要先拆小步。",
            },
        )
        assert write_response.status_code == 201
        entry_id = write_response.json()["entry_id"]

        search_response = await client.post(
            "/v1/memory/search",
            json={"query": "撞牆", "limit": 5, "mode": "lexical"},
        )

    assert search_response.status_code == 200
    payload = search_response.json()
    assert payload["results"][0]["entry"]["entry_id"] == entry_id


@pytest.mark.asyncio
async def test_english_write_search_still_hits_lexical(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "DEC-018 drops mem0 timeout failures with sqlite and ollama.",
            },
        )
        assert write_response.status_code == 201
        entry_id = write_response.json()["entry_id"]

        search_response = await client.post(
            "/v1/memory/search",
            json={"query": "timeout failures", "limit": 5, "mode": "lexical"},
        )

    assert search_response.status_code == 200
    payload = search_response.json()
    assert payload["results"][0]["entry"]["entry_id"] == entry_id


@pytest.mark.asyncio
async def test_mixed_write_search_hits_cjk_english_and_numeric_queries(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "CJK tokenization 影響 2026 roadmap",
            },
        )
        assert write_response.status_code == 201
        entry_id = write_response.json()["entry_id"]

        for query in ("影響", "tokenization", "2026"):
            search_response = await client.post(
                "/v1/memory/search",
                json={"query": query, "limit": 5, "mode": "lexical"},
            )
            assert search_response.status_code == 200
            payload = search_response.json()
            assert payload["results"][0]["entry"]["entry_id"] == entry_id


def test_normalize_fts_query_edge_cases() -> None:
    assert SqliteStore._normalize_fts_query("") == ""
    assert SqliteStore._normalize_fts_query("!!!") == ""

    normalized = SqliteStore._normalize_fts_query("記憶系統 " * 512)
    assert normalized
    assert '"記憶"' in normalized
    assert '"系統"' in normalized


def test_normalize_bm25_preserves_rank_order_for_negative_scores() -> None:
    raw_scores = [-15.0, -10.0, -5.0, -1.0, -0.1]
    normalized = [SqliteStore._normalize_bm25(score) for score in raw_scores]

    assert normalized == sorted(normalized, reverse=True)
    assert normalized[0] == pytest.approx(0.9375)
    assert normalized[1] == pytest.approx(10.0 / 11.0)
    assert normalized[2] == pytest.approx(5.0 / 6.0)
    assert normalized[3] == pytest.approx(0.5)
    assert normalized[4] == pytest.approx(1.0 / 11.0)


@pytest.mark.asyncio
async def test_reindex_fts_rewrites_legacy_rows(tmp_path) -> None:
    settings = build_settings(tmp_path)
    store = SqliteStore(settings.database_path)
    await store.open()
    try:
        created_at = utc_now()
        entry = Entry(
            entry_id="01KPGJIEBATOKENIZERTEST01",
            tenant_id=settings.default_tenant_id,
            agent_id="codex",
            namespace="shared",
            type="note",
            content="最近又撞牆，需要先拆小步。",
            content_hash=build_content_hash("最近又撞牆，需要先拆小步。"),
            summary=None,
            tags=[],
            references=[],
            metadata={},
            sync_status="pending",
            last_embedded_at=None,
            last_embed_error=None,
            last_embed_attempted_at=None,
            embed_attempt_count=0,
            created_at=created_at,
            created_by_principal="pytest",
        )
        outcome = await store.insert_entry(entry)
        assert outcome.created is True

        with sqlite3.connect(settings.database_path) as connection:
            connection.execute(
                "DELETE FROM entries_fts WHERE tenant_id = ? AND entry_id = ?",
                (entry.tenant_id, entry.entry_id),
            )
            connection.execute(
                """
                INSERT INTO entries_fts (entry_id, tenant_id, content, summary, tags)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry.entry_id, entry.tenant_id, entry.content, "", ""),
            )
            connection.commit()

        legacy_hits = await store.search_lexical(entry.tenant_id, "撞牆", limit=5)
        assert legacy_hits == []

        reindexed = await store.reindex_fts_entries([entry])
        assert reindexed == 1

        rebuilt_hits = await store.search_lexical(entry.tenant_id, "撞牆", limit=5)
        assert [hit.entry_id for hit in rebuilt_hits] == [entry.entry_id]

        rerun = await store.reindex_fts_entries([entry])
        assert rerun == 0
    finally:
        await store.close()
