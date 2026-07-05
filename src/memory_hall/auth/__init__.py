from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request

PrincipalRole = Literal["user", "service", "admin"]


@dataclass(slots=True, frozen=True)
class Principal:
    principal_id: str
    role: PrincipalRole = "user"

    @property
    def is_privileged(self) -> bool:
        return self.role in ("service", "admin")


def _auth_error() -> HTTPException:
    return HTTPException(status_code=401, detail="missing or invalid authorization")


async def get_principal(request: Request) -> Principal:
    authorization = request.headers.get("Authorization", "").strip()
    if not authorization:
        if os.getenv("MH_DEV_MODE") == "1":
            return Principal(principal_id="dev-local", role="admin")
        raise _auth_error()

    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            return Principal(principal_id="bearer-user", role="user")
        raise _auth_error()

    if authorization.startswith("HMAC "):
        secret = os.getenv("MH_HMAC_SECRET")
        if not secret:
            raise _auth_error()

        key_material = authorization.removeprefix("HMAC ").strip()
        key_id, separator, signature = key_material.partition(":")
        key_id = key_id.strip()
        signature = signature.strip()
        if not separator or not key_id or not signature:
            raise _auth_error()

        request_body = await request.body()
        expected = hmac.new(secret.encode(), request_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature, expected):
            return Principal(principal_id=key_id, role="service")
        raise _auth_error()

    raise _auth_error()
