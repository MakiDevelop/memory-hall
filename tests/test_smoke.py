from __future__ import annotations

from pathlib import Path

import pytest

from memory_hall.server.app import create_app
from tests.conftest import DeterministicEmbedder, TimeoutEmbedder, build_settings, client_for_app


class CountingEmbedder(DeterministicEmbedder):
    def __init__(self, dim: int = 8) -> None:
        super().__init__(dim=dim)
        self.embed_calls = 0

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return super().embed(text)


@pytest.mark.asyncio
async def test_health_returns_ok(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["storage"] == "ok"


@pytest.mark.asyncio
async def test_health_returns_degraded_when_embedder_unreachable(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["embedder"] == "degraded"


@pytest.mark.asyncio
async def test_health_reads_cached_status_without_reprobing_embedder(app_factory) -> None:
    embedder = CountingEmbedder()
    app = app_factory(embedder=embedder)
    async with client_for_app(app) as client:
        startup_calls = embedder.embed_calls
        response = await client.get("/v1/health")
        assert response.status_code == 200
        assert embedder.embed_calls == startup_calls

        response = await client.get("/v1/health")
        assert response.status_code == 200
        assert embedder.embed_calls == startup_calls


@pytest.mark.asyncio
async def test_write_rejects_oversized_content(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.max_content_bytes = 8
    app = create_app(
        settings=settings,
        embedder=DeterministicEmbedder(dim=settings.vector_dim),
    )
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "123456789",
            },
        )
    assert response.status_code == 413
