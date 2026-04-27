from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from memory_hall.models import Entry, build_content_hash, utc_now
from memory_hall.server.app import create_app
from memory_hall.storage.vector_store import SqliteVecStore
from tests.conftest import DeterministicEmbedder, TimeoutEmbedder, build_settings, client_for_app


class CountingEmbedder(DeterministicEmbedder):
    def __init__(self, dim: int = 8) -> None:
        super().__init__(dim=dim)
        self.embed_calls = 0

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return super().embed(text)


class SlowEmbedder(DeterministicEmbedder):
    def __init__(self, *, sleep_s: float, dim: int = 8) -> None:
        super().__init__(dim=dim)
        self.sleep_s = sleep_s

    def embed(self, text: str) -> list[float]:
        time.sleep(self.sleep_s)
        return super().embed(text)


def _entry(index: int, tenant_id: str) -> Entry:
    content = f"wal checkpoint entry {index}"
    return Entry(
        entry_id=f"01KPWALCHECKPOINT{index:08d}",
        tenant_id=tenant_id,
        agent_id="pytest",
        namespace="shared",
        type="note",
        content=content,
        content_hash=build_content_hash(content),
        summary=None,
        tags=[],
        references=[],
        metadata={},
        sync_status="embedded",
        last_embedded_at=None,
        last_embed_error=None,
        last_embed_attempted_at=None,
        embed_attempt_count=0,
        created_at=utc_now(),
        created_by_principal="pytest",
    )


def _wal_path(path: Path) -> Path:
    return path.with_name(f"{path.name}-wal")


def _wal_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


@pytest.mark.asyncio
async def test_health_returns_ok(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        response = await client.get("/v1/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["storage"] == "ok"
    assert payload["last_error"] is None
    assert payload["last_success_at"] is not None


@pytest.mark.asyncio
async def test_health_returns_degraded_when_embedder_unreachable(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        response = await client.get("/v1/ready")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["embedder"] == "degraded"
    assert payload["last_error"] is not None


@pytest.mark.asyncio
async def test_health_reads_cached_status_without_reprobing_embedder(app_factory) -> None:
    embedder = CountingEmbedder()
    app = app_factory(embedder=embedder)
    async with client_for_app(app) as client:
        startup_calls = embedder.embed_calls
        response = await client.get("/v1/ready")
        assert response.status_code == 200
        assert embedder.embed_calls == startup_calls

        response = await client.get("/v1/ready")
        assert response.status_code == 200
        assert embedder.embed_calls == startup_calls


@pytest.mark.asyncio
async def test_healthz_returns_alive_even_when_embedder_unreachable(app_factory) -> None:
    app = app_factory(embedder=TimeoutEmbedder())
    async with client_for_app(app) as client:
        response = await client.get("/v1/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


@pytest.mark.asyncio
async def test_health_uses_health_embed_timeout(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.embed_timeout_s = 5.0
    settings.health_embed_timeout_s = 0.05
    app = create_app(
        settings=settings,
        embedder=SlowEmbedder(sleep_s=0.2, dim=settings.vector_dim),
    )
    async with client_for_app(app) as client:
        response = await client.get("/v1/ready")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["embedder"] == "degraded"
    assert payload["last_error"] is not None


@pytest.mark.asyncio
async def test_health_logs_subcheck_error_and_exposes_last_error(app_factory, caplog) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        app.state.settings.health_embed_timeout_s = 0.05
        app.state.runtime._health_cache_ttl_s = 0.0
        app.state.runtime.embedder = SlowEmbedder(
            sleep_s=0.2,
            dim=app.state.settings.vector_dim,
        )
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            response = await client.get("/v1/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["last_success_at"] is not None
    assert payload["last_error"] is not None
    assert "embedder" in payload["last_error"]
    assert any(
        "health sub-check failed component=embedder" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_health_alias_matches_ready_response(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        ready_response = await client.get("/v1/ready")
        health_response = await client.get("/v1/health")

    assert ready_response.status_code == health_response.status_code
    assert ready_response.json() == health_response.json()


def test_dockerfile_healthcheck_uses_healthz() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "/v1/healthz" in dockerfile


@pytest.mark.asyncio
async def test_wal_checkpoint_truncates_main_and_vector_wal(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.wal_checkpoint_interval_s = 300.0
    app = create_app(
        settings=settings,
        embedder=DeterministicEmbedder(dim=settings.vector_dim),
    )

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        vector_store = runtime.vector_store
        assert isinstance(vector_store, SqliteVecStore)

        for index in range(100):
            entry = _entry(index, settings.default_tenant_id)
            await runtime.storage.insert_entry(entry)
            await asyncio.to_thread(
                vector_store.upsert,
                entry.tenant_id,
                entry.entry_id,
                [float(index + 1)] * settings.vector_dim,
            )

        main_wal_path = _wal_path(settings.database_path)
        vector_wal_path = _wal_path(settings.vector_database_path)

        assert main_wal_path.exists()
        assert vector_wal_path.exists()
        main_wal_before = _wal_size(main_wal_path)
        vector_wal_before = _wal_size(vector_wal_path)
        assert main_wal_before > 32 * 1024
        assert vector_wal_before > 32 * 1024

        await runtime._checkpoint_wal_databases()

        assert _wal_size(main_wal_path) <= 32 * 1024
        assert _wal_size(vector_wal_path) <= 32 * 1024


@pytest.mark.asyncio
async def test_list_endpoint_accepts_limit_1000_and_rejects_1001(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        response = await client.get("/v1/memory", params={"limit": 1000})
        assert response.status_code == 200

        response = await client.get("/v1/memory", params={"limit": 1001})
        assert response.status_code == 422


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
