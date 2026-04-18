from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(slots=True, frozen=True)
class Principal:
    principal_id: str
    role: str = "admin"


def get_principal(request: Request) -> Principal:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.startswith("HMAC "):
        key_material = authorization.removeprefix("HMAC ").split(":", 1)[0].strip()
        if key_material:
            return Principal(principal_id=key_material)
    return Principal(principal_id="dev-local")
