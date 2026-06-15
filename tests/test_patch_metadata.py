from __future__ import annotations

import pytest

from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_patch_metadata_shallow_merge(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": "project:integration",
                "type": "fact",
                "content": "AMH revoke round-trip test content",
                "metadata": {"amh_status": "active", "source_tier": "llm_derived"},
            },
        )
        assert write_response.status_code == 201
        entry_id = write_response.json()["entry_id"]

        patch_response = await client.patch(
            f"/v1/memory/{entry_id}",
            json={"metadata": {"amh_status": "revoked", "revoked_by": "codex"}},
        )
        assert patch_response.status_code == 200
        patched = patch_response.json()["entry"]["metadata"]
        assert patched["amh_status"] == "revoked"
        assert patched["revoked_by"] == "codex"
        assert patched["source_tier"] == "llm_derived"

        get_response = await client.get(f"/v1/memory/{entry_id}")
        assert get_response.status_code == 200
        assert get_response.json()["entry"]["metadata"]["amh_status"] == "revoked"