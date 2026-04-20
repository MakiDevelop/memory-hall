from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import memory_hall.embedder.http_embedder as http_embedder_module
from memory_hall.config import Settings
from memory_hall.embedder.http_embedder import HttpEmbedder
from memory_hall.server.app import build_runtime


def install_mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(http_embedder_module.httpx, "Client", factory)


def test_embed_single(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"model": "BAAI/bge-m3", "dimension": 4, "count": 1, "dense_vecs": [[1, 2, 3, 4]]},
        )

    install_mock_client(monkeypatch, handler)
    embedder = HttpEmbedder(base_url="http://embedder.test:8790", dim=4)

    assert embedder.embed("hello") == [1.0, 2.0, 3.0, 4.0]
    assert seen_payloads == [{"texts": ["hello"]}]


def test_embed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content.decode("utf-8")) == {"texts": ["hello", "world"]}
        return httpx.Response(
            200,
            json={
                "model": "BAAI/bge-m3",
                "dimension": 3,
                "count": 2,
                "dense_vecs": [[1, 2, 3], [4, 5, 6]],
            },
        )

    install_mock_client(monkeypatch, handler)
    embedder = HttpEmbedder(base_url="http://embedder.test:8790", dim=3)

    assert embedder.embed_batch(["hello", "world"]) == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]


def test_dimension_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"dimension": 512, "count": 1, "dense_vecs": [[1, 2, 3]]})

    install_mock_client(monkeypatch, handler)
    embedder = HttpEmbedder(base_url="http://embedder.test:8790", dim=1024)

    with pytest.raises(ValueError, match="expected dimension 1024, got 512"):
        embedder.embed_batch(["hello"])


def test_http_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"detail": "boom"})

    install_mock_client(monkeypatch, handler)
    embedder = HttpEmbedder(base_url="http://embedder.test:8790")

    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_batch(["hello"])


def test_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_client(*args, **kwargs):
        del args, kwargs
        raise AssertionError("http client should not be constructed for empty input")

    monkeypatch.setattr(http_embedder_module.httpx, "Client", fail_client)
    embedder = HttpEmbedder(base_url="http://embedder.test:8790")

    assert embedder.embed_batch([]) == []


def test_build_runtime_uses_http_embedder(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "memory-hall.sqlite3",
        vector_database_path=tmp_path / "memory-hall-vectors.sqlite3",
        embedder_kind="http",
        embed_base_url="http://embedder.test:8790",
        vector_dim=8,
        embed_timeout_s=2.0,
    )

    runtime = build_runtime(settings=settings)

    assert isinstance(runtime.embedder, HttpEmbedder)
    assert runtime.embedder.base_url == "http://embedder.test:8790"
    assert runtime.embedder.timeout_s == 10.0
    assert runtime.embedder.dim == 8
