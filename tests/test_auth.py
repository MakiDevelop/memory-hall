from __future__ import annotations

from pathlib import Path

import pytest

from memory_hall.server.app import create_app
from tests.conftest import DeterministicEmbedder, build_settings, client_for_app


def _write_payload() -> dict[str, object]:
    return {
        "agent_id": "claude",
        "namespace": "home",
        "type": "note",
        "content": "auth-test",
    }


@pytest.mark.asyncio
async def test_auth_disabled_allows_unauthenticated_write(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    assert settings.api_token is None
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/memory/write", json=_write_payload())
    assert response.status_code in (200, 201, 202)


@pytest.mark.asyncio
async def test_auth_empty_string_token_also_disables_auth(tmp_path: Path) -> None:
    # docker-compose `${MH_API_TOKEN:-}` expands to "" when host env is unset.
    # pydantic reads that as "" (not None). Middleware must treat "" like None.
    settings = build_settings(tmp_path)
    settings.api_token = ""
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/memory/write", json=_write_payload())
    assert response.status_code in (200, 201, 202)


@pytest.mark.asyncio
async def test_auth_enabled_missing_header_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/memory/write", json=_write_payload())
    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


@pytest.mark.asyncio
async def test_auth_enabled_wrong_token_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"


@pytest.mark.asyncio
async def test_auth_enabled_valid_token_allows_write(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Bearer secret-token-abc"},
        )
    assert response.status_code in (200, 201, 202)


@pytest.mark.asyncio
async def test_auth_enabled_wrong_scheme_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Basic secret-token-abc"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


@pytest.mark.asyncio
async def test_auth_enabled_health_endpoint_stays_public(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
    # Health returns 200 (or 503 degraded). Point is: not 401.
    assert response.status_code != 401


@pytest.mark.asyncio
async def test_auth_enabled_search_requires_token(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/search",
            json={"query": "anything", "mode": "hybrid", "limit": 5},
        )
    assert response.status_code == 401
