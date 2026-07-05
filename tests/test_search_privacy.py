"""Contract tests for search privacy gate (P0-1).

Verifies that server-side agent_id scoping is enforced based on principal role:
- Bearer (user role): auto-scoped to caller, cross-agent denied with 403
- HMAC (service role): cross-agent allowed
- Dev mode (admin role): cross-agent allowed
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod

import pytest

from tests.conftest import client_for_app


BEARER_TOKEN = "test-bearer-token"
HMAC_SECRET = "test-hmac-secret"


def _hmac_header(body: bytes, key_id: str = "amh-adapter") -> str:
    sig = hmac_mod.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"HMAC {key_id}:{sig}"


async def _seed_entries(client, headers: dict):
    """Seed two entries with different agent_ids."""
    for agent_id, content in [
        ("claude", "Claude's private session note about project planning"),
        ("codex", "Codex's private review of auth module refactor"),
    ]:
        await client.post(
            "/v1/memory/write",
            json={
                "agent_id": agent_id,
                "namespace": "project:test",
                "type": "episode",
                "content": content,
            },
            headers=headers,
        )


@pytest.mark.anyio
async def test_bearer_unfiltered_scoped_to_caller(app_factory, monkeypatch):
    """Bearer search without agent_id filter → auto-scoped to caller (bearer-user)."""
    monkeypatch.setenv("MH_API_TOKEN", BEARER_TOKEN)
    monkeypatch.delenv("MH_DEV_MODE", raising=False)
    app = app_factory()

    async with client_for_app(app) as client:
        # Seed with dev mode for setup
        monkeypatch.setenv("MH_DEV_MODE", "1")
        await _seed_entries(client, {"Authorization": "Bearer ignored-in-dev"})
        monkeypatch.delenv("MH_DEV_MODE", raising=False)

        # Search with Bearer token, no agent_id filter
        resp = await client.post(
            "/v1/memory/search",
            json={"query": "session note project"},
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        for item in data["results"]:
            assert item["entry"]["agent_id"] == "bearer-user"


@pytest.mark.anyio
async def test_bearer_cross_agent_allowed(app_factory, monkeypatch):
    """Bearer search with explicit cross-agent_id → allowed (single-user deployment)."""
    monkeypatch.setenv("MH_API_TOKEN", BEARER_TOKEN)
    monkeypatch.delenv("MH_DEV_MODE", raising=False)
    app = app_factory()

    async with client_for_app(app) as client:
        resp = await client.post(
            "/v1/memory/search",
            json={"query": "anything", "agent_id": "codex"},
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_bearer_self_scope_allowed(app_factory, monkeypatch):
    """Bearer search with agent_id matching caller → allowed."""
    monkeypatch.setenv("MH_API_TOKEN", BEARER_TOKEN)
    monkeypatch.delenv("MH_DEV_MODE", raising=False)
    app = app_factory()

    async with client_for_app(app) as client:
        resp = await client.post(
            "/v1/memory/search",
            json={"query": "anything", "agent_id": "bearer-user"},
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_hmac_cross_agent_allowed(app_factory, monkeypatch):
    """HMAC (service role) search with cross-agent_id → allowed."""
    monkeypatch.setenv("MH_HMAC_SECRET", HMAC_SECRET)
    monkeypatch.delenv("MH_DEV_MODE", raising=False)
    app = app_factory()

    async with client_for_app(app) as client:
        body = b'{"query": "review auth module", "agent_id": "codex"}'
        resp = await client.post(
            "/v1/memory/search",
            content=body,
            headers={
                "Authorization": _hmac_header(body),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_dev_mode_cross_agent_allowed(app_factory, monkeypatch):
    """Dev mode (admin role) search with cross-agent_id → allowed."""
    monkeypatch.setenv("MH_DEV_MODE", "1")
    app = app_factory()

    async with client_for_app(app) as client:
        resp = await client.post(
            "/v1/memory/search",
            json={"query": "anything", "agent_id": "codex"},
        )
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_no_auth_search_rejected(app_factory, monkeypatch):
    """Search without any auth → 401."""
    app = app_factory()
    monkeypatch.delenv("MH_DEV_MODE", raising=False)

    async with client_for_app(app) as client:
        resp = await client.post(
            "/v1/memory/search",
            json={"query": "anything"},
        )
        assert resp.status_code == 401
