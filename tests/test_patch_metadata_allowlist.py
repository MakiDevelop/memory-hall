from __future__ import annotations

import pytest

from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_patch_metadata_rejects_non_amh_keys(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": "project:integration",
                "type": "fact",
                "content": "allowlist test",
                "metadata": {"amh_status": "active", "importance": 5},
            },
        )
        entry_id = write_response.json()["entry_id"]

        patch_response = await client.patch(
            f"/v1/memory/{entry_id}",
            json={
                "metadata": {
                    "amh_status": "revoked",
                    "importance": 9,
                    "evil": "injected",
                }
            },
        )
        assert patch_response.status_code == 200
        meta = patch_response.json()["entry"]["metadata"]
        assert meta["amh_status"] == "revoked"
        assert meta["importance"] == 5
        assert "evil" not in meta