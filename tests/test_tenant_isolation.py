from __future__ import annotations

import pytest
from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_synthetic_tenant_b_cannot_read_tenant_a_entries(app_factory, tmp_path) -> None:
    app_a = app_factory(tenant_id="default", base_dir=tmp_path / "shared")
    async with client_for_app(app_a) as client_a:
        write = await client_a.post(
            "/v1/memory/write",
            json={
                "agent_id": "claude",
                "namespace": "shared",
                "type": "note",
                "content": "tenant a secret decision",
            },
        )
        entry_id = write.json()["entry_id"]

    app_b = app_factory(tenant_id="tenant-b", base_dir=tmp_path / "shared")
    async with client_for_app(app_b) as client_b:
        list_response = await client_b.get("/v1/memory", params={"limit": 10})
        search_response = await client_b.post(
            "/v1/memory/search",
            json={"query": "secret decision", "limit": 10},
        )
        get_response = await client_b.get(f"/v1/memory/{entry_id}")

    assert list_response.status_code == 200
    assert list_response.json()["entries"] == []
    assert search_response.status_code == 200
    assert search_response.json()["results"] == []
    assert get_response.status_code == 404
