from __future__ import annotations

import pytest
from tests.conftest import client_for_app


@pytest.mark.asyncio
async def test_health_returns_ok(app_factory) -> None:
    app = app_factory()
    async with client_for_app(app) as client:
        response = await client.get("/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["storage"] == "ok"
