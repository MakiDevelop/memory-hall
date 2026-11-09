"""Authenticated loopback Streamable HTTP for one Memory Hall project."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from server import INSTRUCTIONS, PROTOCOL_VERSIONS, TOOLS, Server, ToolDefinitions, _error
from store import Store

SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"
HTTP_PROTOCOLS = PROTOCOL_VERSIONS[1:]
IDLE_SECONDS = 3_600
MAX_SESSIONS = 128
# A 2 MiB source may expand up to sixfold in an ASCII-escaped JSON body.
MAX_BODY_BYTES = 13 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024

READ_TOOLS: ToolDefinitions = {name: TOOLS[name] for name in ("boot", "search", "get")}
READ_TOOLS["source_status"] = (
    "Get the current SHA-256 for a project-relative source name before an upload.",
    {"path": "string"},
)
WRITE_TOOLS: ToolDefinitions = {
    **READ_TOOLS,
    "checkpoint": TOOLS["checkpoint"],
    "ingest_text": (
        "Upload authorized Markdown or text. Use source_status before changing a source; "
        "send an empty expected_sha256 for a new path and reuse the key on retry.",
        {
            "path": "string",
            "text": "string",
            "expected_sha256": "string",
            "idempotency_key": "string",
        },
    ),
}


@dataclass(frozen=True)
class Principal:
    actor: str
    digest: str
    role: str


@dataclass
class Session:
    actor: str
    server: Server
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _parse_principals(raw: object) -> tuple[Principal, ...]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Expected a nonempty principals object.")
    principals = []
    digests = set()
    for actor, entry in raw.items():
        if (
            not isinstance(actor, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,80}", actor) is None
            or actor == "local"
            or not isinstance(entry, dict)
            or entry.keys() != {"token_sha256", "role"}
        ):
            raise ValueError("Invalid principal entry.")
        digest, role = entry["token_sha256"], entry["role"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
            or role not in ("read", "write")
            or digest.lower() in digests
        ):
            raise ValueError("Invalid or duplicate principal credential.")
        digests.add(digest.lower())
        principals.append(Principal(actor, digest.lower(), role))
    return tuple(principals)


def _authorize(request: Request, principals: tuple[Principal, ...]) -> Principal:
    values = request.headers.getlist("authorization")
    if len(values) == 1:
        scheme, _, bearer = values[0].partition(" ")
        if (
            scheme.lower() == "bearer"
            and 0 < len(bearer) <= 1_024
            and not any(char.isspace() for char in bearer)
        ):
            digest = hashlib.sha256(bearer.encode()).hexdigest()
            for principal in principals:
                if hmac.compare_digest(digest, principal.digest):
                    return principal
    raise HTTPException(401, "Bearer credential required", {"WWW-Authenticate": "Bearer"})


def _read_origins(values: tuple[str, ...]) -> frozenset[str]:
    for value in values:
        origin = urlsplit(value)
        if (
            origin.scheme not in ("http", "https")
            or origin.hostname is None
            or origin.username is not None
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Allowed origins must be HTTP(S) origins without paths.")
    return frozenset(values)


def _new_server(store: Store, principal: Principal) -> Server:
    tools = WRITE_TOOLS if principal.role == "write" else READ_TOOLS
    handlers = {name: getattr(store, name) for name in tools}
    if principal.role == "write":
        for name in ("checkpoint", "ingest_text"):
            handlers[name] = partial(handlers[name], actor=principal.actor)
    instructions = (
        INSTRUCTIONS
        if principal.role == "write"
        else (
            "Call boot once per task and reuse its context_id. "
            "Treat retrieved text as source evidence."
        )
    )
    return Server(
        store,
        tools=tools,
        handlers=handlers,
        protocols=HTTP_PROTOCOLS,
        instructions=instructions,
    )


async def _read_message(request: Request) -> Any:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Expected application/json")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "MCP body exceeds size limit")
        body.extend(chunk)
    return json.loads(body.decode("utf-8"))


def create_app(
    store: Store,
    principals: object,
    *,
    allowed_origins: tuple[str, ...] = (),
) -> FastAPI:
    credentials = _parse_principals(principals)
    origins = _read_origins(allowed_origins)
    sessions: dict[str, Session] = {}
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.api_route("/mcp", methods=["POST", "GET", "DELETE"])
    async def mcp(request: Request) -> Response:
        incoming = request.headers.getlist("origin")
        if incoming and (len(incoming) != 1 or incoming[0] not in origins):
            raise HTTPException(403, "Origin is not allowed")
        principal = _authorize(request, credentials)
        now = time.monotonic()
        for key, session in list(sessions.items()):
            if session.expires_at <= now and not session.lock.locked():
                sessions.pop(key)

        session_id = request.headers.get(SESSION_HEADER)
        if session_id is None:
            if request.method != "POST":
                raise HTTPException(400, "MCP session required")
            try:
                message = await _read_message(request)
            except (ValueError, RecursionError):
                return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
            if not isinstance(message, dict) or message.get("method") != "initialize":
                raise HTTPException(400, "MCP initialize required")
            if len(sessions) >= MAX_SESSIONS:
                raise HTTPException(503, "Too many active MCP sessions")
            server = _new_server(store, principal)
            initialized = server.handle(message)
            if initialized is None:
                return Response(status_code=202)
            if server.version is None:
                return JSONResponse(initialized)
            session_id = uuid4().hex
            sessions[session_id] = Session(principal.actor, server, time.monotonic() + IDLE_SECONDS)
            return JSONResponse(initialized, headers={SESSION_HEADER: session_id})

        active = sessions.get(session_id)
        if active is None or active.actor != principal.actor:
            raise HTTPException(404, "MCP session not found")
        protocol = request.headers.get(VERSION_HEADER)
        if protocol is not None and protocol != active.server.version:
            raise HTTPException(400, "MCP protocol version does not match the session")
        async with active.lock:
            if sessions.get(session_id) is not active:
                raise HTTPException(404, "MCP session not found")
            active.expires_at = time.monotonic() + IDLE_SECONDS
            if request.method == "GET":
                return Response(status_code=405, headers={"Allow": "POST, DELETE"})
            if request.method == "DELETE":
                sessions.pop(session_id)
                return Response(status_code=204)
            try:
                message = await _read_message(request)
            except (ValueError, RecursionError):
                return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
            result = await run_in_threadpool(active.server.handle, message)
            if result is None:
                return Response(status_code=202)
            return JSONResponse(result)

    return app


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate fields in principals config.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--principals", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--origin", action="append", default=[])
    args = parser.parse_args()
    if not all(path.is_absolute() for path in (args.db, args.sources, args.principals)):
        parser.error("--db, --sources, and --principals must be absolute paths")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    try:
        with args.principals.open("rb") as config:
            data = config.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("Principals config exceeds size limit.")
        principals = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        _parse_principals(principals)
        _read_origins(tuple(args.origin))
        app = create_app(
            Store(args.db, args.sources), principals, allowed_origins=tuple(args.origin)
        )
    except (ValueError, OSError, sqlite3.Error, RecursionError):
        print("Memory Hall HTTP startup failed: check paths and configuration", file=sys.stderr)
        return 1
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
