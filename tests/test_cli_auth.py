from __future__ import annotations

from memory_hall.cli.main import _client


def test_client_attaches_bearer_header_when_api_token_set(monkeypatch) -> None:
    monkeypatch.setenv("MH_API_TOKEN", "secret-token-abc")

    with _client("http://127.0.0.1:9000", 5.0) as client:
        assert client.headers["Authorization"] == "Bearer secret-token-abc"


def test_client_omits_bearer_header_when_api_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("MH_API_TOKEN", raising=False)

    with _client("http://127.0.0.1:9000", 5.0) as client:
        assert "Authorization" not in client.headers


def test_client_omits_bearer_header_when_api_token_empty(monkeypatch) -> None:
    monkeypatch.setenv("MH_API_TOKEN", "")

    with _client("http://127.0.0.1:9000", 5.0) as client:
        assert "Authorization" not in client.headers
