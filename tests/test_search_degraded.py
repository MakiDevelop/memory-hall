from __future__ import annotations

import pytest

from tests.conftest import TimeoutEmbedder, client_for_app


@pytest.mark.asyncio
async def test_hybrid_search_marks_timeout_degradation(app_factory) -> None:
    app = app_factory(hybrid_mode="weighted_linear")
    async with client_for_app(app) as client:
        write_response = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "hybrid timeout fallback note",
            },
        )
        assert write_response.status_code == 201

        app.state.settings.search_embed_timeout_s = 0.05
        app.state.runtime.embedder = TimeoutEmbedder(
            sleep_s=0.2,
            dim=app.state.settings.vector_dim,
        )

        search_response = await client.post(
            "/v1/memory/search",
            json={
                "query": "timeout fallback",
                "limit": 5,
                "mode": "hybrid",
            },
        )

    assert search_response.status_code == 200
    payload = search_response.json()
    assert payload["degraded"] is True
    assert payload["results"][0]["entry"]["content"] == "hybrid timeout fallback note"
    assert payload["results"][0]["score_breakdown"]["semantic_status"] == "timeout"
    assert payload["results"][0]["score_breakdown"]["hybrid_mode"] == "weighted_linear"
    assert payload["results"][0]["score_breakdown"]["alpha"] == pytest.approx(1.0)
    assert payload["results"][0]["score"] == pytest.approx(
        payload["results"][0]["score_breakdown"]["bm25"]
    )
