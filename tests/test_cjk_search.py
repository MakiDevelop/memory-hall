from __future__ import annotations

import pytest

from tests.conftest import DeterministicEmbedder, client_for_app


class CJKBiasEmbedder(DeterministicEmbedder):
    def embed(self, text: str) -> list[float]:
        if text == "撞牆":
            return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        if "blocked" in text.lower():
            return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.5]
        if "撞牆" in text:
            return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.1]
        return super().embed(text)


@pytest.mark.asyncio
async def test_short_cjk_query_prefers_fts_match(app_factory) -> None:
    app = app_factory(embedder=CJKBiasEmbedder())
    async with client_for_app(app) as client:
        chinese = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "最近又撞牆，需要先拆小步。",
            },
        )
        blocked = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "The rollout is blocked but does not use the Chinese term.",
            },
        )
        response = await client.post(
            "/v1/memory/search",
            json={"query": "撞牆", "limit": 5, "mode": "hybrid"},
        )

    assert chinese.status_code == 201
    assert blocked.status_code == 201
    payload = response.json()
    assert payload["results"][0]["entry"]["content"].startswith("最近又撞牆")
    assert payload["results"][0]["score_breakdown"]["bm25"] > 0
