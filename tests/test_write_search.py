from __future__ import annotations

import pytest

from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_write_search_get_and_list_filters(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "project:memory-hall",
                "type": "decision",
                "content": "DEC-018 drops mem0 timeout failures with sqlite and ollama.",
                "summary": "drop mem0 write path LLM calls",
                "tags": ["governance", "mem0"],
                "references": [],
                "metadata": {"importance": 4},
            },
        )
        assert write_response.status_code == 201
        entry_id = write_response.json()["entry_id"]

        search_response = await client.post(
            "/v1/memory/search",
            json={
                "query": "mem0 timeout",
                "namespace": ["project:memory-hall"],
                "type": ["decision"],
                "limit": 10,
                "mode": "hybrid",
            },
        )
        assert search_response.status_code == 200
        search_payload = search_response.json()
        assert search_payload["degraded"] is False
        assert search_payload["results"][0]["entry"]["entry_id"] == entry_id
        assert search_payload["results"][0]["score_breakdown"]["semantic_status"] == "ok"

        get_response = await client.get(f"/v1/memory/{entry_id}")
        assert get_response.status_code == 200
        get_payload = get_response.json()
        assert get_payload["entry"]["content"].startswith("DEC-018")
        assert get_payload["references_out"] == []
        assert get_payload["references_in"] == []

        list_response = await client.get(
            "/v1/memory",
            params={
                "namespace": "project:memory-hall",
                "agent_id": "codex",
                "type": "decision",
                "limit": 10,
            },
        )
        assert list_response.status_code == 200
        list_payload = list_response.json()
        assert len(list_payload["entries"]) == 1
        assert list_payload["entries"][0]["entry_id"] == entry_id
