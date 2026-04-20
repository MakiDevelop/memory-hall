from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from memory_hall.config import Settings
from memory_hall.server.app import create_app


class DeterministicEmbedder:
    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.timeout_s = 2.0

    def embed(self, text: str) -> list[float]:
        lower = text.lower()
        base = [0.0] * self.dim
        keywords = [
            "mem0",
            "timeout",
            "tenant",
            "sqlite",
            "ollama",
            "pending",
            "撞牆",
            "blocked",
        ]
        for index, keyword in enumerate(keywords[: self.dim]):
            if keyword == "撞牆":
                base[index] = float(text.count(keyword))
            else:
                base[index] = float(lower.count(keyword))
        checksum = sum(ord(char) for char in text) % 97
        base[-1] += checksum / 100.0
        return base

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class TimeoutEmbedder(DeterministicEmbedder):
    def embed(self, text: str) -> list[float]:
        del text
        time.sleep(3)
        return super().embed("")


def build_settings(tmp_path: Path, tenant_id: str = "default", dim: int = 8) -> Settings:
    return Settings(
        database_path=tmp_path / "memory-hall.sqlite3",
        vector_database_path=tmp_path / "memory-hall-vectors.sqlite3",
        vector_dim=dim,
        health_embed_timeout_s=1.0,
        default_tenant_id=tenant_id,
        request_timeout_s=1.0,
    )


@asynccontextmanager
async def client_for_app(app) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture()
def deterministic_embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder()


@pytest.fixture()
def app_factory(tmp_path: Path):
    def factory(*, tenant_id: str = "default", embedder=None, base_dir: Path | None = None):
        root = base_dir or tmp_path
        settings = build_settings(root, tenant_id=tenant_id)
        active_embedder = embedder or DeterministicEmbedder(dim=settings.vector_dim)
        return create_app(settings=settings, embedder=active_embedder)

    return factory
