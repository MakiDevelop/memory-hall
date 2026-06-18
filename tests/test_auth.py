from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from memory_hall.server.app import ProductionAuthError, create_app
from tests.conftest import DeterministicEmbedder, build_settings, client_for_app


def _write_payload() -> dict[str, object]:
    return {
        "agent_id": "claude",
        "namespace": "home",
        "type": "note",
        "content": "auth-test",
    }


def _json_body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _hmac_header(*, key_id: str, secret: str, body: bytes) -> str:
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"HMAC {key_id}:{signature}"


def test_production_auth_guard_rejects_non_loopback_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MH_ALLOW_INSECURE", raising=False)
    settings = build_settings(tmp_path)
    settings.host = "0.0.0.0"  # noqa: S104 - intentional unsafe bind test input.
    settings.api_token = None

    with pytest.raises(
        ProductionAuthError,
        match=r"Refusing to start: binding to 0\.0\.0\.0 without MH_API_TOKEN",
    ):
        create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))


def test_production_auth_guard_allows_non_loopback_with_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MH_ALLOW_INSECURE", raising=False)
    settings = build_settings(tmp_path)
    settings.host = "0.0.0.0"  # noqa: S104 - intentional unsafe bind test input.
    settings.api_token = "secret-token-abc"

    create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))


def test_production_auth_guard_allows_explicit_insecure_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MH_ALLOW_INSECURE", "1")
    settings = build_settings(tmp_path)
    settings.host = "0.0.0.0"  # noqa: S104 - intentional unsafe bind test input.
    settings.api_token = None

    create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))


def test_production_auth_guard_allows_localhost_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MH_ALLOW_INSECURE", raising=False)
    settings = build_settings(tmp_path)
    settings.host = "localhost"
    settings.api_token = None

    create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))


@pytest.mark.asyncio
async def test_missing_authorization_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    assert settings.api_token is None
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/memory/write", json=_write_payload())
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or invalid authorization"


@pytest.mark.asyncio
async def test_dev_mode_allows_unauthenticated_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # docker-compose `${MH_API_TOKEN:-}` expands to "" when host env is unset.
    # pydantic reads that as "" (not None). Middleware must treat "" like None.
    monkeypatch.setenv("MH_DEV_MODE", "1")
    settings = build_settings(tmp_path)
    settings.api_token = ""
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/memory/write", json=_write_payload())
        entry_response = await client.get(f"/v1/memory/{response.json()['entry_id']}")
    assert response.status_code in (200, 201, 202)
    assert entry_response.status_code == 200
    assert entry_response.json()["entry"]["created_by_principal"] == "dev-local"


@pytest.mark.asyncio
async def test_bearer_auth_uses_bearer_user_principal(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Bearer secret-token-abc"},
        )
        entry_response = await client.get(
            f"/v1/memory/{response.json()['entry_id']}",
            headers={"Authorization": "Bearer secret-token-abc"},
        )
    assert response.status_code in (200, 201, 202)
    assert entry_response.status_code == 200
    assert entry_response.json()["entry"]["created_by_principal"] == "bearer-user"


@pytest.mark.asyncio
async def test_hmac_auth_valid_signature_uses_key_id_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "hmac-secret"
    monkeypatch.setenv("MH_HMAC_SECRET", secret)
    settings = build_settings(tmp_path)
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    body = _json_body(_write_payload())
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            content=body,
            headers={
                "Authorization": _hmac_header(key_id="agent-key", secret=secret, body=body),
                "Content-Type": "application/json",
            },
        )
        entry_response = await client.get(f"/v1/memory/{response.json()['entry_id']}")
    assert response.status_code in (200, 201, 202)
    assert entry_response.status_code == 200
    assert entry_response.json()["entry"]["created_by_principal"] == "agent-key"


@pytest.mark.asyncio
async def test_hmac_auth_wrong_signature_returns_401(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MH_HMAC_SECRET", "hmac-secret")
    settings = build_settings(tmp_path)
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    body = _json_body(_write_payload())
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            content=body,
            headers={
                "Authorization": "HMAC agent-key:wrong",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or invalid authorization"


@pytest.mark.asyncio
async def test_auth_enabled_missing_header_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/memory/write", json=_write_payload())
    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


@pytest.mark.asyncio
async def test_auth_enabled_wrong_token_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"


@pytest.mark.asyncio
async def test_auth_enabled_valid_token_allows_write(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Bearer secret-token-abc"},
        )
    assert response.status_code in (200, 201, 202)


@pytest.mark.asyncio
async def test_auth_enabled_wrong_scheme_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Basic secret-token-abc"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


@pytest.mark.asyncio
async def test_auth_enabled_health_endpoints_stay_public(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
        assert response.status_code != 401


@pytest.mark.asyncio
async def test_auth_enabled_search_requires_token(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/search",
            json={"query": "anything", "mode": "hybrid", "limit": 5},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_uses_scope_path_not_host_spoofed_health_path(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "secret-token-abc"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Host": "testserver/v1/health?"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


# ---------- ADR 0009: admin gate (two-tier bearer) ------------------------


@pytest.mark.asyncio
async def test_admin_token_unset_admin_falls_back_to_api_token(tmp_path: Path) -> None:
    """Backward compat: when MH_ADMIN_TOKEN is unset, /v1/admin/* uses api_token."""
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = None
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/admin/audit",
            headers={"Authorization": "Bearer shared-token"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_token_set_correct_token_allows_admin(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = "admin-only-token"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/admin/audit",
            headers={"Authorization": "Bearer admin-only-token"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_token_set_api_token_rejected_on_admin(tmp_path: Path) -> None:
    """When admin_token is set, the regular api_token must NOT grant admin access."""
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = "admin-only-token"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/admin/audit",
            headers={"Authorization": "Bearer shared-token"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid admin token"


@pytest.mark.asyncio
async def test_admin_token_set_missing_header_returns_401(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = "admin-only-token"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post("/v1/admin/audit")
    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


@pytest.mark.asyncio
async def test_admin_gate_uses_scope_path_not_host_spoofed_non_admin_path(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = "admin-only-token"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/admin/audit",
            headers={
                "Authorization": "Bearer shared-token",
                "Host": "testserver/v1/memory/write?",
            },
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid admin token"


@pytest.mark.asyncio
async def test_admin_token_does_not_grant_general_endpoints(tmp_path: Path) -> None:
    """admin_token is admin-only; it must not work as a general api_token on
    non-admin paths (least privilege both directions)."""
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = "admin-only-token"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.post(
            "/v1/memory/write",
            json=_write_payload(),
            headers={"Authorization": "Bearer admin-only-token"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_token_set_health_endpoints_stay_public(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.api_token = "shared-token"
    settings.admin_token = "admin-only-token"
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
        assert response.status_code != 401


# ---------- ADR 0009: config-level invariants (fail-fast) ----------------


def test_settings_admin_token_without_api_token_fails(tmp_path: Path) -> None:
    """Codex review finding #1 [HIGH]: admin_token set + api_token unset would
    fail-open on non-admin paths. Settings load must reject this combo."""
    from pydantic import ValidationError

    from memory_hall.config import Settings

    with pytest.raises(ValidationError, match="admin_token requires api_token"):
        Settings(
            database_path=tmp_path / "db.sqlite3",
            vector_database_path=tmp_path / "vec.sqlite3",
            admin_token="admin-only-token",
            api_token=None,
        )


def test_settings_admin_token_equal_to_api_token_fails(tmp_path: Path) -> None:
    """Codex review finding #2 [MEDIUM]: equal tokens silently nullify the
    two-tier separation. Settings load must reject this combo."""
    from pydantic import ValidationError

    from memory_hall.config import Settings

    with pytest.raises(ValidationError, match="admin_token must differ from api_token"):
        Settings(
            database_path=tmp_path / "db.sqlite3",
            vector_database_path=tmp_path / "vec.sqlite3",
            api_token="same-token",
            admin_token="same-token",
        )
