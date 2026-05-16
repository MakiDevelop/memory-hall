from __future__ import annotations

from memory_hall.cli.main import _client, _preview, _query_params


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


def test_query_params_omits_none_and_expands_lists() -> None:
    assert _query_params(
        limit=5,
        namespace=["project:memory-hall", "shared"],
        agent_id=None,
        type=["episode"],
        tags=None,
    ) == [
        ("limit", 5),
        ("namespace", "project:memory-hall"),
        ("namespace", "shared"),
        ("type", "episode"),
    ]


def test_preview_compacts_and_truncates_long_content() -> None:
    value = "first line\n\nsecond line " + ("x" * 200)

    preview = _preview(value, max_chars=40)

    assert preview == "first line second line xxxxxxxxxxxxxxxx…"
    assert "\n" not in preview
