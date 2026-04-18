from __future__ import annotations

import pytest
from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_duplicate_content_returns_existing_entry_id(app_factory) -> None:
    app = app_factory()
    payload = {
        "agent_id": "codex",
        "namespace": "shared",
        "type": "note",
        "content": "same content should dedup per tenant",
        "tags": ["dup"],
    }
    async with client_for_app(app) as client:
        first = await client.post("/v1/memory/write", json=payload)
        second = await client.post("/v1/memory/write", json=payload)
        listed = await client.get("/v1/memory", params={"limit": 10})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["entry_id"] == second.json()["entry_id"]
    assert len(listed.json()["entries"]) == 1
