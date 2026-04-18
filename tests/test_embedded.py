from __future__ import annotations

from pathlib import Path

import pytest

from memory_hall import MemoryHallRuntime, Settings, build_runtime
from memory_hall.models import SYNC_EMBEDDED, SearchMemoryRequest, WriteMemoryRequest

from .conftest import DeterministicEmbedder, build_settings


@pytest.mark.asyncio
async def test_embedded_write_and_search_without_http(tmp_path: Path) -> None:
    """Sandboxed agents that cannot open sockets can still use memory-hall as a library.

    The scenario: Codex / Gemini / other agents in a restricted sandbox that blocks
    localhost TCP. They should be able to import the runtime and write / search
    directly, no HTTP required.
    """
    settings = build_settings(tmp_path)
    runtime = build_runtime(
        settings=settings,
        embedder=DeterministicEmbedder(dim=settings.vector_dim),
    )
    assert isinstance(runtime, MemoryHallRuntime)

    await runtime.start()
    try:
        write_result = await runtime.write_entry(
            tenant_id=settings.default_tenant_id,
            principal_id="embedded-agent",
            payload=WriteMemoryRequest(
                agent_id="codex",
                namespace="shared",
                type="note",
                content="Sandboxed agent writing via in-process API — no socket needed.",
                tags=["memory-hall", "embedded"],
            ),
        )
        assert write_result.entry.entry_id.startswith("01")
        assert write_result.entry.sync_status == SYNC_EMBEDDED

        search_result = await runtime.search_entries(
            tenant_id=settings.default_tenant_id,
            payload=SearchMemoryRequest(query="sandboxed", limit=5),
        )
        assert search_result.total >= 1
        assert any(
            hit.entry.entry_id == write_result.entry.entry_id
            for hit in search_result.results
        )

        get_result = await runtime.get_entry(
            tenant_id=settings.default_tenant_id,
            entry_id=write_result.entry.entry_id,
        )
        assert get_result is not None
        assert get_result.entry.agent_id == "codex"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_embedded_package_exports_stable_surface() -> None:
    import memory_hall

    assert "MemoryHallRuntime" in memory_hall.__all__
    assert "Settings" in memory_hall.__all__
    assert "build_runtime" in memory_hall.__all__
    assert "create_app" in memory_hall.__all__
    assert isinstance(memory_hall.Settings(), Settings)
