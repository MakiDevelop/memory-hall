"""HTTP contract test mimicking AMH MemhallStore (integration:memhall-amh Phase 3)."""

from __future__ import annotations

import uuid

import pytest

from memory_hall.models import build_content_hash
from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_amh_contract_write_read_revoke_supersede_link(app_factory) -> None:
    app = app_factory()
    ns = f"project:amh-contract-{uuid.uuid4().hex[:8]}"
    content_parent = f"parent-{uuid.uuid4().hex}"
    content_child = f"child-{uuid.uuid4().hex}"

    async with client_for_app(app) as client:
        parent_write = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": ns,
                "type": "fact",
                "content": content_parent,
                "tags": ["amh"],
                "metadata": {
                    "amh_status": "active",
                    "amh_content_hash": "deadbeef",
                },
            },
        )
        assert parent_write.status_code in {200, 201, 202}
        parent_id = parent_write.json()["entry_id"]

        child_write = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": ns,
                "type": "fact",
                "content": content_child,
                "tags": ["amh"],
                "metadata": {
                    "amh_status": "active",
                    "supersedes": parent_id,
                },
            },
        )
        assert child_write.status_code in {200, 201, 202}
        child_id = child_write.json()["entry_id"]

        patch_parent = await client.patch(
            f"/v1/memory/{parent_id}",
            json={"metadata": {"amh_status": "superseded"}},
        )
        assert patch_parent.status_code == 200
        assert patch_parent.json()["entry"]["metadata"]["amh_status"] == "superseded"

        link = await client.post(
            f"/v1/memory/{child_id}/link",
            json={"target_entry_id": parent_id, "relation": "supersedes"},
        )
        assert link.status_code == 200

        revoke_target = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "amh",
                "namespace": ns,
                "type": "fact",
                "content": f"revoke-{uuid.uuid4().hex}",
                "metadata": {"amh_status": "active"},
            },
        )
        revoke_id = revoke_target.json()["entry_id"]
        revoke_patch = await client.patch(
            f"/v1/memory/{revoke_id}",
            json={"metadata": {"amh_status": "revoked", "revoked_by": "contract-test"}},
        )
        assert revoke_patch.status_code == 200
        assert revoke_patch.json()["entry"]["metadata"]["amh_status"] == "revoked"

        by_hash = await client.get(
            "/v1/memory/by-hash",
            params={
                "content_hash": build_content_hash(content_parent),
                "namespace": ns,
            },
        )
        assert by_hash.status_code == 200
        assert by_hash.json()["entry"]["entry_id"] == parent_id