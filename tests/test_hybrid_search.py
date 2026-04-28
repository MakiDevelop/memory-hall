from __future__ import annotations

from pathlib import Path

import pytest

from memory_hall.server.app import create_app
from tests.conftest import DeterministicEmbedder, build_settings, client_for_app


class WeightedHybridEmbedder(DeterministicEmbedder):
    def embed(self, text: str) -> list[float]:
        base = [0.0] * self.dim
        lower = text.lower()
        base[0] = 1.0 if "quokkamode" in lower else 0.0
        base[1] = 1.0 if "rollout" in lower else 0.0
        base[2] = 1.0 if any(
            token in lower for token in ("resurface", "restore", "recovery")
        ) else 0.0
        base[3] = 1.0 if any(token in lower for token in ("checklist", "list")) else 0.0
        base[4] = 1.0 if (
            lower.strip() in {"hybrid search", "hybrid ranking"}
            or "combined retrieval" in lower
            or "ranking strategy" in lower
        ) else 0.0
        base[5] = 1.0 if (
            lower.strip() in {"hybrid search", "hybrid ranking"}
            or ("hybrid" in lower and "combined retrieval" in lower)
        ) else 0.0
        base[-1] = 0.01
        return base


@pytest.mark.asyncio
async def test_weighted_linear_prefers_rare_lexical_target(app_factory) -> None:
    app = app_factory(embedder=WeightedHybridEmbedder(), hybrid_mode="weighted_linear")
    async with client_for_app(app) as client:
        target = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "quokkamode rollout mitigation log",
            },
        )
        distractor = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "rollout playbook for tomorrow morning",
            },
        )
        response = await client.post(
            "/v1/memory/search",
            json={"query": "quokkamode rollout", "limit": 5, "mode": "hybrid"},
        )

    assert target.status_code == 201
    assert distractor.status_code == 201
    payload = response.json()
    assert payload["results"][0]["entry"]["entry_id"] == target.json()["entry_id"]
    assert payload["results"][0]["score_breakdown"]["hybrid_mode"] == "weighted_linear"
    assert payload["results"][0]["score_breakdown"]["alpha"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_weighted_linear_recovers_semantic_paraphrase_without_lexical_overlap(
    app_factory,
) -> None:
    app = app_factory(embedder=WeightedHybridEmbedder(), hybrid_mode="weighted_linear")
    async with client_for_app(app) as client:
        relevant = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "restore recovery list after embed failures",
            },
        )
        await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "release calendar for next month",
            },
        )
        await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "daily standup reminders",
            },
        )
        response = await client.post(
            "/v1/memory/search",
            json={"query": "resurface checklist", "limit": 5, "mode": "hybrid"},
        )

    assert relevant.status_code == 201
    payload = response.json()
    top_three_ids = [item["entry"]["entry_id"] for item in payload["results"][:3]]
    assert relevant.json()["entry_id"] in top_three_ids
    assert payload["results"][0]["entry"]["entry_id"] == relevant.json()["entry_id"]
    assert payload["results"][0]["score_breakdown"]["alpha"] == pytest.approx(0.0)
    assert payload["results"][0]["score_breakdown"]["bm25"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_weighted_linear_rewards_entries_that_hit_both_signals(app_factory) -> None:
    app = app_factory(embedder=WeightedHybridEmbedder(), hybrid_mode="weighted_linear")
    async with client_for_app(app) as client:
        lexical_only = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "hybrid ranking marker",
            },
        )
        semantic_only = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "combined retrieval ranking strategy",
            },
        )
        both = await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "hybrid combined retrieval ranking strategy",
            },
        )
        response = await client.post(
            "/v1/memory/search",
            json={"query": "hybrid ranking", "limit": 5, "mode": "hybrid"},
        )

    assert lexical_only.status_code == 201
    assert semantic_only.status_code == 201
    assert both.status_code == 201
    payload = response.json()
    score_by_id = {item["entry"]["entry_id"]: item["score"] for item in payload["results"]}
    both_id = both.json()["entry_id"]
    lexical_only_id = lexical_only.json()["entry_id"]
    semantic_only_id = semantic_only.json()["entry_id"]

    assert payload["results"][0]["entry"]["entry_id"] == both_id
    assert score_by_id[both_id] > score_by_id[lexical_only_id]
    assert score_by_id[both_id] > score_by_id[semantic_only_id]
    assert score_by_id[both_id] - max(
        score_by_id[lexical_only_id],
        score_by_id[semantic_only_id],
    ) > 0.05


@pytest.mark.asyncio
async def test_hybrid_search_supports_legacy_rrf_mode(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.hybrid_mode = "rrf"
    app = create_app(
        settings=settings,
        embedder=WeightedHybridEmbedder(dim=settings.vector_dim),
    )

    async with client_for_app(app) as client:
        await client.post(
            "/v1/memory/write",
            json={
                "agent_id": "codex",
                "namespace": "shared",
                "type": "note",
                "content": "hybrid combined retrieval ranking strategy",
            },
        )
        response = await client.post(
            "/v1/memory/search",
            json={"query": "hybrid search", "limit": 5, "mode": "hybrid"},
        )

    payload = response.json()
    assert payload["results"][0]["score_breakdown"]["hybrid_mode"] == "rrf"
