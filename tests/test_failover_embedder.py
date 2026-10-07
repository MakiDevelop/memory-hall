from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import httpx
import pytest

import memory_hall.embedder.failover_embedder as failover_module
from memory_hall.config import Settings
from memory_hall.embedder.failover_embedder import FailoverEmbedder
from memory_hall.embedder.http_embedder import HttpEmbedder
from memory_hall.models import WriteMemoryRequest
from memory_hall.server.app import build_runtime, create_app
from tests.conftest import build_settings, client_for_app
from tests.test_http_embedder import install_mock_client


def make_embedder():
    return FailoverEmbedder([
        HttpEmbedder(base_url=f"http://{host}.test", dim=2)
        for host in ("dgx", "mini2", "mini1")
    ])


def success(request):
    if request.url.path == "/health":
        return httpx.Response(200, json={"model": "BAAI/bge-m3", "dimension": 2})
    count = len(json.loads(request.content)["texts"])
    return httpx.Response(200, json={"dimension": 2, "dense_vecs": [[1, 2]] * count})


def test_primary_order_batch_and_no_repeated_probe(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path))
        assert request.extensions["timeout"]["connect"] == 2.0
        assert request.extensions["timeout"]["read"] == 8.0
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    assert embedder.embed_batch([]) == []
    assert embedder.embed_batch(["a", "b"]) == [[1, 2], [1, 2]]
    assert embedder.embed("c") == [1, 2]
    assert seen == [("dgx.test", "/health"), ("dgx.test", "/embed"), ("dgx.test", "/embed")]


@pytest.mark.parametrize("failure", ["connect", "connect_timeout", "read_timeout", "5xx",
                                     "invalid", "json", "dimension", "count", "nan"])
@pytest.mark.parametrize("phase", ["/health", "/embed"])
def test_failover_and_cooldown(monkeypatch, failure, phase):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path))
        if request.url.host == "dgx.test" and request.url.path == phase:
            errors = {"connect": httpx.ConnectError, "connect_timeout": httpx.ConnectTimeout,
                      "read_timeout": httpx.ReadTimeout}
            if failure in errors:
                raise errors[failure]("offline", request=request)
            if failure == "5xx":
                return httpx.Response(503)
            if failure == "json":
                return httpx.Response(200, content=b"not json")
            if failure == "dimension":
                return httpx.Response(200, json={"model": "BAAI/bge-m3", "dimension": 3,
                                               "dense_vecs": [[1, 2, 3]]})
            if failure == "count":
                return httpx.Response(200, json={"dimension": 2, "dense_vecs": []})
            if failure == "nan":
                return httpx.Response(200, content=b'{"dimension":2,"dense_vecs":[[NaN,2]]}')
            return httpx.Response(200, json=[])
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    assert embedder.embed("a") == [1, 2]
    seen.clear()
    assert embedder.embed("b") == [1, 2]
    assert seen == [("mini2.test", "/embed")]
    snapshot = embedder.health_snapshot()
    assert snapshot["last_embed_backend"] == "http://mini2.test"
    expected = "mismatch" if phase == "/health" and failure == "dimension" else "cooling_down"
    assert snapshot["embed_backends"][0]["state"] == expected


def test_recovery_rechecks_primary_and_shared_timeout_view(monkeypatch):
    now = [100.0]
    seen = []
    monkeypatch.setattr(failover_module.time, "monotonic", lambda: now[0])

    def handler(request):
        seen.append((request.url.host, request.url.path))
        if request.url.host == "dgx.test" and now[0] < 160:
            raise httpx.ConnectError("down")
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    view = embedder.clone_with_timeout(3)
    assert view.embed("a") == [1, 2]
    assert embedder.health_snapshot()["last_embed_backend"] == "http://mini2.test"
    now[0] = 161
    seen.clear()
    assert embedder.embed("b") == [1, 2]
    assert seen == [("dgx.test", "/health"), ("dgx.test", "/embed")]
    assert view.health_snapshot()["last_embed_backend"] == "http://dgx.test"


@pytest.mark.parametrize("field,value", [("model", "other-model"), ("dimension", 99)])
def test_mismatch_rechecked_and_logs_once(monkeypatch, caplog, field, value):
    seen = []
    now = [100.0]
    monkeypatch.setattr(failover_module.time, "monotonic", lambda: now[0])

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "dgx.test":
            assert request.url.path == "/health"
            return httpx.Response(200, json={"model": "BAAI/bge-m3", "dimension": 2,
                                            field: value})
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    with caplog.at_level(logging.INFO, logger=failover_module.__name__):
        embedder.embed("a")
        now[0] = 1000
        embedder.embed("b")
    assert seen.count("dgx.test") == 2
    assert sum("state=mismatch" in record.message for record in caplog.records) == 1
    assert embedder.health_snapshot()["embed_backends"][0]["state"] == "mismatch"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, ValueError,
                                        httpx.HTTPStatusError])
def test_all_down_preserves_error_type_even_during_cooldown(monkeypatch, error_type):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if error_type is httpx.HTTPStatusError:
            return httpx.Response(500)
        if error_type is ValueError:
            return httpx.Response(200, json=[])
        raise error_type("down", request=request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    for _ in range(2):
        with pytest.raises(error_type):
            embedder.embed("a")
    assert seen == ["dgx.test", "mini2.test", "mini1.test"]


def test_config_env_parsing_and_conflict(monkeypatch):
    monkeypatch.setenv("MH_EMBEDDER_KIND", "http")
    monkeypatch.setenv("MH_EMBED_BASE_URL", "")
    monkeypatch.setenv("MH_EMBED_BASE_URLS", " http://dgx.test/ , http://mini2.test, ")
    settings = Settings(_env_file=None)
    assert settings.http_embed_urls == ["http://dgx.test", "http://mini2.test"]
    assert settings.embed_connect_timeout_s == 2
    assert settings.embed_cooldown_s == 60
    assert settings.embed_mismatch_recheck_s == 600
    monkeypatch.setenv("MH_EMBED_MISMATCH_RECHECK_S", "900")
    assert Settings(_env_file=None).embed_mismatch_recheck_s == 900
    assert settings.embed_model == "BAAI/bge-m3"
    monkeypatch.setenv("MH_EMBED_BASE_URL", "http://single.test")
    with pytest.raises(ValueError, match="set only one"):
        Settings(_env_file=None)
    monkeypatch.setenv("MH_EMBED_BASE_URLS", "")
    assert Settings(_env_file=None).http_embed_urls == ["http://single.test"]
    monkeypatch.setenv("MH_EMBEDDER_KIND", "ollama")
    monkeypatch.setenv("MH_EMBED_BASE_URLS", "http://dgx.test")
    assert Settings(_env_file=None).embedder_kind == "ollama"


@pytest.mark.parametrize("url", ["ftp://host", "http://user:secret@host", "http://host?token=secret",
                                 "http://host#secret", ", ,"])
def test_config_rejects_invalid_or_secret_urls(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None, embedder_kind="http", embed_base_urls=url)


async def test_health_output_and_all_down_write_retry(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ConnectError("secret must not be exposed", request=request)

    install_mock_client(monkeypatch, handler)
    monkeypatch.setenv("MH_DEV_MODE", "1")
    settings = build_settings(tmp_path, dim=2)
    embedder = make_embedder()
    app = create_app(settings=settings, embedder=embedder)
    async with client_for_app(app) as client:
        health = await client.get("/v1/health")
        assert health.status_code == 503
        assert health.json()["last_embed_backend"] is None
        assert [item["state"] for item in health.json()["embed_backends"]] == ["cooling_down"] * 3
        assert "secret" not in health.text
        response = await client.post("/v1/memory/write", json={
            "agent_id": "codex", "namespace": "shared", "type": "note", "content": "all down",
        })
        assert response.status_code == 202
        assert response.json()["embedded"] is False
        runtime = app.state.runtime
        entries = await runtime.storage.list_pending_entries("default")
        assert len(entries) == 1
        assert entries[0].embed_attempt_count == 1
        assert entries[0].last_embed_error.startswith("ConnectError:")
        for count in range(2, 6):
            await runtime._embed_reindex_batch(entries)
            entry = await runtime.storage.get_entry("default", entries[0].entry_id)
            assert entry.embed_attempt_count == count
            entries = [entry]
        assert entry.sync_status == "failed"


async def test_health_success_reports_fallback_and_live_last_backend(monkeypatch, tmp_path):
    def handler(request):
        if request.url.host == "dgx.test":
            return httpx.Response(200, json={"model": "wrong", "dimension": 2})
        return success(request)

    install_mock_client(monkeypatch, handler)
    monkeypatch.setenv("MH_DEV_MODE", "1")
    app = create_app(settings=build_settings(tmp_path, dim=2), embedder=make_embedder())
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["last_embed_backend"] == "http://mini2.test"
    assert [item["state"] for item in response.json()["embed_backends"]] == [
        "mismatch", "healthy", "cooling_down",
    ]


async def test_factory_write_can_fail_over(monkeypatch, tmp_path):
    def handler(request):
        if request.url.host == "dgx.test":
            raise httpx.ConnectTimeout("offline")
        return success(request)

    install_mock_client(monkeypatch, handler)
    settings = build_settings(tmp_path, dim=2)
    settings.embedder_kind = "http"
    settings.embed_base_urls = "http://dgx.test,http://mini2.test"
    settings.embed_mismatch_recheck_s = 900
    runtime = build_runtime(settings=settings)
    assert runtime.embedder.mismatch_recheck_s == 900
    await runtime.start()
    try:
        outcome = await runtime.write_entry(tenant_id="default", principal_id="test",
                                            payload=WriteMemoryRequest(
                                                agent_id="codex", namespace="shared", type="note",
                                                content="fallback works"))
        assert outcome.status_code == 201
        assert outcome.embedded
        assert runtime._embed_timeout_s() == runtime.embedder.timeout_s
    finally:
        await runtime.stop()


def test_healthy_backend_failure_then_recovery_mismatch(monkeypatch, caplog):
    now = [100.0]
    mode = ["healthy"]
    seen = []
    monkeypatch.setattr(failover_module.time, "monotonic", lambda: now[0])

    def handler(request):
        seen.append((request.url.host, request.url.path))
        if request.url.host == "dgx.test":
            if mode[0] == "down":
                raise httpx.ConnectError("down")
            if mode[0] == "changed":
                assert request.url.path == "/health"
                return httpx.Response(200, json={"model": "different", "dimension": 2})
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    with caplog.at_level(logging.INFO, logger=failover_module.__name__):
        embedder.embed("a")
        mode[0] = "down"
        embedder.embed("b")
        embedder.embed("c")
        now[0] = 161
        mode[0] = "changed"
        embedder.embed("d")
    primary_logs = [record.message.rsplit("=", 1)[1] for record in caplog.records
                    if "backend=http://dgx.test " in record.message]
    assert primary_logs == ["healthy", "cooling_down", "mismatch"]
    assert seen.count(("dgx.test", "/health")) == 2
    assert embedder.health_snapshot()["last_embed_backend"] == "http://mini2.test"


def test_4xx_preserves_status_error_without_fallback(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(401, json={"detail": "secret"})

    install_mock_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        make_embedder().embed("a")
    assert caught.value.response.status_code == 401
    assert "secret" not in str(caught.value)
    assert seen == ["dgx.test"]


def test_expired_view_stops_before_trying_remaining_nodes(monkeypatch):
    now = [100.0]
    seen = []
    monkeypatch.setattr(failover_module.time, "monotonic", lambda: now[0])

    def handler(request):
        seen.append(request.url.host)
        now[0] += 4
        raise httpx.ReadTimeout("too slow")

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder().clone_with_timeout(3)
    with pytest.raises(httpx.ReadTimeout):
        embedder.embed("a")
    assert seen == ["dgx.test"]
    assert embedder._shared.backends[1].retry_at == 0


@pytest.mark.parametrize("warm", [False, True])
def test_parallel_embeds_and_single_flight_probe(monkeypatch, warm):
    seen = []
    rendezvous = Barrier(2)
    start = Barrier(2)
    concurrent = [False]

    def handler(request):
        seen.append(request.url.path)
        if concurrent[0]:
            if request.url.path == "/health":
                time.sleep(0.02)
            else:
                # This barrier fails if any lock spans an embed HTTP request.
                rendezvous.wait(timeout=2)
                time.sleep(0.2)
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = FailoverEmbedder([HttpEmbedder(base_url="http://dgx.test", dim=2)])
    if warm:
        embedder.embed("warmup")
    concurrent[0] = True

    def run(view):
        start.wait(timeout=2)
        began = time.monotonic()
        result = view.embed("parallel")
        return result, time.monotonic() - began

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, view) for view in
                   (embedder, embedder.clone_with_timeout(2))]
        results = [future.result(timeout=3) for future in futures]
    assert [result for result, _ in results] == [[1, 2], [1, 2]]
    assert max(elapsed for _, elapsed in results) < 0.38
    assert seen.count("/health") == 1


@pytest.mark.parametrize("field,value", [("model", "wrong"), ("dimension", 99)])
def test_fixed_mismatch_rejoins_after_separate_interval(monkeypatch, field, value):
    now = [100.0]
    fixed = [False]
    seen = []
    monkeypatch.setattr(failover_module.time, "monotonic", lambda: now[0])

    def handler(request):
        seen.append((request.url.host, request.url.path))
        if request.url.host == "dgx.test" and not fixed[0]:
            assert request.url.path == "/health"
            return httpx.Response(200, json={"model": "BAAI/bge-m3", "dimension": 2,
                                            field: value})
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    embedder.mismatch_recheck_s = 900
    embedder.embed("mismatch")
    fixed[0] = True
    now[0] = 999
    seen.clear()
    embedder.embed("still excluded")
    assert seen == [("mini2.test", "/embed")]
    assert embedder.health_snapshot()["embed_backends"][0]["state"] == "mismatch"
    now[0] = 1000
    seen.clear()
    embedder.clone_with_timeout(2).embed("recovered")
    assert seen == [("dgx.test", "/health"), ("dgx.test", "/embed")]
    assert embedder.health_snapshot()["embed_backends"][0]["state"] == "healthy"


def test_busy_probe_skipped_without_blocking_search(monkeypatch):
    probing = Event()
    release = Event()
    probes = []

    def handler(request):
        if request.url.path == "/health":
            probes.append(request.url.host)
            if request.url.host == "dgx.test":
                probing.set()
                assert release.wait(timeout=2)
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(embedder.embed, "write")
        try:
            assert probing.wait(timeout=2)
            assert embedder.clone_with_timeout(0.5).embed("search") == [1, 2]
            assert embedder.health_snapshot()["last_embed_backend"] == "http://mini2.test"
            assert probes.count("dgx.test") == 1
        finally:
            release.set()
        assert first.result(timeout=2) == [1, 2]


def test_late_embed_success_does_not_clear_concurrent_failure(monkeypatch):
    started = Event()
    release = Event()

    def handler(request):
        if request.url.path == "/embed" and request.url.host == "dgx.test":
            text = json.loads(request.content)["texts"][0]
            if text == "slow":
                started.set()
                assert release.wait(timeout=2)
            elif text == "fail":
                raise httpx.ConnectError("offline")
        return success(request)

    install_mock_client(monkeypatch, handler)
    embedder = make_embedder()
    embedder.embed("warmup")
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(embedder.embed, "slow")
        try:
            assert started.wait(timeout=2)
            assert embedder.embed("fail") == [1, 2]
        finally:
            release.set()
        assert slow.result(timeout=2) == [1, 2]
    assert embedder.health_snapshot()["embed_backends"][0]["state"] == "cooling_down"
