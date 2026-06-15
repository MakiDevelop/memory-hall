from __future__ import annotations

import uuid

import pytest

from memory_hall.models import build_content_hash
from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_lookup_by_amh_content_hash(app_factory) -> None:
    app = app_factory()
    ns = f"project:lookup-{uuid.uuid4().hex[:8]}"
    amh_hash = "abc123def456"
    content = f"lookup-{uuid.uuid4().hex}"

    async with client_for_app(app) as client:
        write = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": ns,
                "type": "fact",
                "content": content,
                "metadata": {"amh_content_hash": amh_hash, "amh_status": "active"},
            },
        )
        entry_id = write.json()["entry_id"]

        lookup = await client.get(
            "/v1/memory/by-amh-hash",
            params={"namespace": ns, "hash": amh_hash},
        )
        assert lookup.status_code == 200
        assert lookup.json()["entry"]["entry_id"] == entry_id

        sha_lookup = await client.get(
            "/v1/memory/by-hash",
            params={"content_hash": build_content_hash(content), "namespace": ns},
        )
        assert sha_lookup.status_code == 200
        assert sha_lookup.json()["entry"]["entry_id"] == entry_id