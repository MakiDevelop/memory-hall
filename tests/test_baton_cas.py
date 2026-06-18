from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memory_hall.storage.sqlite_store import SqliteStore
from tests.conftest import client_for_app


async def _write_baton(client, baton: dict, expected_revision: int | None = None):
    payload = {
        "namespace": "loop",
        "baton": baton,
    }
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    return await client.post("/v1/baton/write", json=payload)


@pytest.mark.asyncio
async def test_basic_read_write_tracks_revision(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        write_response = await _write_baton(client, {"step": "first"})
        read_response = await client.post("/v1/baton/read", json={"namespace": "loop"})

    assert write_response.status_code == 200
    assert write_response.json()["revision"] == 1
    assert read_response.status_code == 200
    assert read_response.json()["baton"] == {"step": "first"}
    assert read_response.json()["revision"] == 1
    assert read_response.json()["updated_at"] == write_response.json()["updated_at"]


@pytest.mark.asyncio
async def test_cas_success_with_correct_expected_revision(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        initial_response = await _write_baton(client, {"step": "first"})
        revision = initial_response.json()["revision"]
        update_response = await _write_baton(
            client,
            {"step": "second"},
            expected_revision=revision,
        )

    assert update_response.status_code == 200
    assert update_response.json()["revision"] == 2


@pytest.mark.asyncio
async def test_cas_conflict_returns_409_with_current_baton(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        await _write_baton(client, {"step": "first"})
        conflict_response = await _write_baton(
            client,
            {"step": "stale-write"},
            expected_revision=99,
        )

    assert conflict_response.status_code == 409
    detail = conflict_response.json()["detail"]
    assert detail["error"] == "baton_revision_conflict"
    assert detail["namespace"] == "loop"
    assert detail["expected_revision"] == 99
    assert detail["current_baton"] == {"step": "first"}
    assert detail["current_revision"] == 1
    assert detail["current_updated_at"] is not None


@pytest.mark.asyncio
async def test_lww_backward_compat_without_expected_revision(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        await _write_baton(client, {"step": "first"})
        stale_compatible_response = await _write_baton(client, {"step": "second"})
        read_response = await client.post("/v1/baton/read", json={"namespace": "loop"})

    assert stale_compatible_response.status_code == 200
    assert stale_compatible_response.json()["revision"] == 2
    assert read_response.json()["baton"] == {"step": "second"}


@pytest.mark.asyncio
async def test_tenant_b_cannot_read_tenant_a_baton(app_factory, tmp_path: Path) -> None:
    app_a = app_factory(tenant_id="default", base_dir=tmp_path / "shared")
    async with client_for_app(app_a) as client_a:
        write_response = await _write_baton(client_a, {"secret": "tenant-a"})

    app_b = app_factory(tenant_id="tenant-b", base_dir=tmp_path / "shared")
    async with client_for_app(app_b) as client_b:
        read_response = await client_b.post("/v1/baton/read", json={"namespace": "loop"})
        write_b_response = await _write_baton(client_b, {"secret": "tenant-b"})

    app_a_again = app_factory(tenant_id="default", base_dir=tmp_path / "shared")
    async with client_for_app(app_a_again) as client_a_again:
        read_a_again = await client_a_again.post("/v1/baton/read", json={"namespace": "loop"})

    assert write_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json()["baton"] is None
    assert read_response.json()["revision"] is None
    assert write_b_response.status_code == 200
    assert write_b_response.json()["revision"] == 1
    assert read_a_again.json()["baton"] == {"secret": "tenant-a"}
    assert read_a_again.json()["revision"] == 1


@pytest.mark.asyncio
async def test_new_baton_gets_revision_one(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        response = await _write_baton(client, {"step": "first"})

    assert response.status_code == 200
    assert response.json()["revision"] == 1


@pytest.mark.asyncio
async def test_revision_increments_on_each_write(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        first = await _write_baton(client, {"step": "first"})
        second = await _write_baton(client, {"step": "second"})
        third = await _write_baton(client, {"step": "third"})

    assert first.json()["revision"] == 1
    assert second.json()["revision"] == 2
    assert third.json()["revision"] == 3


@pytest.mark.asyncio
async def test_legacy_batons_table_migrates_to_default_tenant(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE batons (
                namespace TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO batons (namespace, updated_at, data)
            VALUES ('loop', '2026-06-18T00:00:00+00:00', '{"step":"legacy"}')
            """
        )

    store = SqliteStore(database_path)
    await store.open()
    try:
        default_baton = await store.baton_read("default", "loop")
        tenant_b_baton = await store.baton_read("tenant-b", "loop")
    finally:
        await store.close()

    assert default_baton == (
        {"step": "legacy"},
        "2026-06-18T00:00:00+00:00",
        1,
    )
    assert tenant_b_baton == (None, None, None)
