from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp


class TenantMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, tenant_id: str = "default") -> None:
        super().__init__(app)
        self.tenant_id = tenant_id

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.headers.get("X-Tenant-Override") is not None:
            return JSONResponse(
                status_code=400,
                content={"detail": "X-Tenant-Override is not available in v0.1"},
            )
        request.state.tenant_id = self.tenant_id
        return await call_next(request)
