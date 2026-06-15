from __future__ import annotations

import pytest

from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_link_adds_reference_edge(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        parent = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": "project:integration",
                "type": "fact",
                "content": "parent memory",
            },
        )
        child = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": "project:integration",
                "type": "fact",
                "content": "child memory",
            },
        )
        parent_id = parent.json()["entry_id"]
        child_id = child.json()["entry_id"]

        link_response = await client.post(
            f"/v1/memory/{child_id}/link",
            json={"target_entry_id": parent_id, "relation": "supersedes"},
        )
        assert link_response.status_code == 200

        get_response = await client.get(f"/v1/memory/{child_id}")
        refs_out = get_response.json()["references_out"]
        assert any(item["entry_id"] == parent_id for item in refs_out)