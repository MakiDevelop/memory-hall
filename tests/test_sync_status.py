from __future__ import annotations

import pytest

from tests.conftest import DeterministicEmbedder, TimeoutEmbedder, client_for_app


class BatchTrackingEmbedder(DeterministicEmbedder):
    def __init__(self, dim: int = 8) -> None:
        super().__init__(dim=dim)
        self.embed_calls = 0
        self.embed_batch_calls: list[int] = []

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return super().embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.embed_batch_calls.append(len(texts))
        return [DeterministicEmbedder.embed(self, text) for text in texts]


@pytest.mark.asyncio
async def test_pending_write_reindexes_to_embedded(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "pending entry waiting for embedder",
            },
        )
        assert write_response.status_code == 202
        payload = write_response.json()
        assert payload["sync_status"] == "pending"

        app.state.runtime.embedder = DeterministicEmbedder(dim=app.state.settings.vector_dim)
        reindex_response = await client.post("/v1/admin/reindex")
        assert reindex_response.status_code == 200
        assert reindex_response.json()["embedded"] >= 1

        get_response = await client.get(f"/v1/memory/{payload['entry_id']}")
        assert get_response.status_code == 200
        assert get_response.json()["entry"]["sync_status"] == "embedded"


@pytest.mark.asyncio
async def test_reindex_uses_embed_batch_for_pending_backlog(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        for index in range(3):
            response = await client.post(
                "/v1/memory/write",
                json={
                    "agent_id": "codex",
                    "namespace": "shared",
                    "type": "note",
                    "content": f"pending batch entry {index}",
                },
            )
            assert response.status_code == 202

        tracking = BatchTrackingEmbedder(dim=app.state.settings.vector_dim)
        app.state.runtime.embedder = tracking

        reindex_response = await client.post("/v1/admin/reindex")
        assert reindex_response.status_code == 200
        assert reindex_response.json()["embedded"] == 3
        assert tracking.embed_batch_calls == [3]
        assert tracking.embed_calls == 0
