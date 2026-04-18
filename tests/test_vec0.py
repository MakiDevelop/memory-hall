from __future__ import annotations

from pathlib import Path

import pytest

from memory_hall.storage.vector_store import SqliteVecStore


def test_vec0_engaged_on_platforms_that_support_it(tmp_path: Path) -> None:
    """Guard against silently regressing to brute-force Python cosine.

    On the supported platforms (macOS / Linux with stdlib sqlite3 + sqlite-vec
    installed), the vec0 virtual table path must be active. If this assertion
    ever fails, someone removed the dependency or broke extension loading.
    """
    store = SqliteVecStore(tmp_path / "v.sqlite3", dim=4)
    store.open()
    try:
        assert store._vec0_enabled, "expected sqlite-vec vec0 to load; falling back is a regression"
    finally:
        store.close()


def test_vec0_cosine_similarity_semantics(tmp_path: Path) -> None:
    """Exact-match vector should score 1.0; orthogonal direction should score 0."""
    store = SqliteVecStore(tmp_path / "v.sqlite3", dim=4)
    store.open()
    try:
        store.upsert("default", "exact", [1.0, 0.0, 0.0, 0.0])
        store.upsert("default", "ortho", [0.0, 1.0, 0.0, 0.0])
        hits = store.search("default", [1.0, 0.0, 0.0, 0.0], k=5)
        by_id = {hit.entry_id: hit.score for hit in hits}
        assert pytest.approx(by_id["exact"], abs=1e-4) == 1.0
        assert pytest.approx(by_id["ortho"], abs=1e-4) == 0.0
    finally:
        store.close()


def test_vec0_tenant_isolation_in_vector_index(tmp_path: Path) -> None:
    """Search in tenant A must not return tenant B's entries.

    In practice entry_id is a globally unique ULID so the ids never collide.
    This test verifies the search-side partition filter: even when both tenants
    have vectors close to the query, only the calling tenant's entries come back.
    """
    store = SqliteVecStore(tmp_path / "v.sqlite3", dim=4)
    store.open()
    try:
        store.upsert("tenant-a", "entry-a", [1.0, 0.0, 0.0, 0.0])
        store.upsert("tenant-b", "entry-b", [1.0, 0.0, 0.0, 0.0])
        hits_a = store.search("tenant-a", [1.0, 0.0, 0.0, 0.0], k=5)
        hits_b = store.search("tenant-b", [1.0, 0.0, 0.0, 0.0], k=5)
        assert [h.entry_id for h in hits_a] == ["entry-a"]
        assert [h.entry_id for h in hits_b] == ["entry-b"]
    finally:
        store.close()
