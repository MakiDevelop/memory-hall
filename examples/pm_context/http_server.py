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
from server import (
    EXTENDED_TOOLS,
    INSTRUCTIONS,
    METADATA_FIELDS,
    PROTOCOL_VERSIONS,
    TOOLS,
    RepeatGuard,
    Server,
    ToolDefinitions,
    _error,
    handoff_destinations,
)
from store import Store

SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"
HTTP_PROTOCOLS = PROTOCOL_VERSIONS[1:]
IDLE_SECONDS = 3_600
MAX_SESSIONS = 128
MAX_SESSIONS_PER_PRINCIPAL = 16
# A 2 MiB source may expand up to sixfold in an ASCII-escaped JSON body.
MAX_BODY_BYTES = 13 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024

READ_TOOLS: ToolDefinitions = {name: TOOLS[name] for name in ("boot", "search", "get")}
READ_TOOLS["source_status"] = (
    "Get the current SHA-256 for a project-relative source name before an upload.",
    {"path": "string", "namespace": "string?"},
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
            **METADATA_FIELDS,
            "summary_mode": "string?",
        },
    ),
}


@dataclass(frozen=True)
class Principal:
    actor: str
    digest: str
    role: str
    namespaces: tuple[str, ...] = ("default",)
    default_namespace: str = "default"


@dataclass
class Session:
    actor: str
    server: Server
    expires_at: float
    principal: Principal | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _parse_principals(raw: object, default_namespace: str = "default") -> tuple[Principal, ...]:
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
            or not {"token_sha256", "role"} <= entry.keys()
            or entry.keys() - {"token_sha256", "role", "namespaces", "default_namespace"}
        ):
            raise ValueError("Invalid principal entry.")
        digest, role = entry["token_sha256"], entry["role"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
            or role not in ("read", "write", "reader", "agent_writer", "pm")
            or digest.lower() in digests
        ):
            raise ValueError("Invalid or duplicate principal credential.")
        namespaces = entry.get("namespaces", [default_namespace])
        selected = entry.get("default_namespace", default_namespace)
        if (
            not isinstance(namespaces, list)
            or not 1 <= len(namespaces) <= 32
            or any(
                not isinstance(ns, str) or re.fullmatch(r"[A-Za-z0-9:._-]{1,96}", ns) is None
                for ns in namespaces
            )
            or not isinstance(selected, str)
            or selected not in namespaces
        ):
            raise ValueError("Invalid namespace scope or default namespace.")
        digests.add(digest.lower())
        principals.append(Principal(actor, digest.lower(), role, tuple(namespaces), selected))
    return tuple(principals)


class PrincipalRegistry:
    """Read administrator-owned credentials on each request, failing closed."""

    def __init__(self, source: object, default_namespace: str):
        self.source = source
        self.default_namespace = default_namespace
        self.current()

    def current(self) -> tuple[Principal, ...]:
        raw = self.source
        if isinstance(raw, Path):
            with raw.open("rb") as handle:
                data = handle.read(MAX_CONFIG_BYTES + 1)
            if len(data) > MAX_CONFIG_BYTES:
                raise ValueError("Principals config exceeds size limit.")
            raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        return _parse_principals(raw, self.default_namespace)


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


def _invoke(store: Store, principal: Principal, tool: str, summarizer: Any, **arguments) -> dict:
    if tool == "precheck":
        from safety import precheck

        return precheck(arguments)
    if tool == "handoff_template":
        from handoff_template import template

        return {"status": "ok", "body": template()}
    namespace = arguments.get("namespace")
    if namespace is None and "context_id" in arguments:
        namespace = store.context_namespace(arguments["context_id"], actor=principal.actor)
    if namespace is None:
        namespace = principal.default_namespace
    if namespace not in principal.namespaces:
        raise ValueError("Namespace access denied.")
    arguments["namespace"] = namespace
    arguments["actor"] = principal.actor
    if tool == "ingest_text":
        from safety import require_safe

        # Scan before either a model or the store receives user-provided content.
        require_safe(arguments["text"])
        for value in arguments.values():
            if isinstance(value, str):
                require_safe(value)
        mode = arguments.pop("summary_mode", "extractive")
        if mode not in ("extractive", "manual", "model"):
            raise ValueError("Invalid summary_mode.")
        if mode == "model":
            if "summary" in arguments:
                raise ValueError("Model summary_mode does not accept a manual summary.")
        elif "summary" in arguments:
            arguments["summary_kind"] = "manual"
        elif mode == "manual":
            raise ValueError("Manual summary_mode requires summary.")
        if arguments.get("record_type") in ("decision", "requirement") and principal.role != "pm":
            if arguments.get("lifecycle", "proposed") != "proposed":
                raise ValueError("Only the PM role can publish active decisions.")
            arguments["lifecycle"] = "proposed"
        arguments["allow_active_decisions"] = principal.role == "pm"
        if mode == "model":
            return store.model_ingest_text(summarizer=summarizer, **arguments)
    return getattr(store, tool)(**arguments)


def _new_server(
    store: Store,
    principal: Principal,
    *,
    extended: bool = False,
    summarizer: Any = None,
    guard: RepeatGuard | None = None,
) -> Server:
    reader = principal.role in ("read", "reader")
    tools = dict(READ_TOOLS if reader else WRITE_TOOLS)
    if extended:
        names = ("digest", "precheck", "handoff_template", "handoff_read", "handoff_status")
        if not reader:
            names += ("handoff_write", "handoff_retry")
        tools.update({name: EXTENDED_TOOLS[name] for name in names})
    handlers = {name: partial(_invoke, store, principal, name, summarizer) for name in tools}
    instructions = (
        INSTRUCTIONS
        if not reader
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
        guard=guard,
        actor=principal.actor,
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
    extended: bool = False,
    summarizer: Any = None,
    guard: RepeatGuard | None = None,
    max_sessions_per_principal: int = MAX_SESSIONS_PER_PRINCIPAL,
) -> FastAPI:
    guard = guard or RepeatGuard()
    registry = PrincipalRegistry(principals, store.default_namespace)
    origins = _read_origins(allowed_origins)
    sessions: dict[str, Session] = {}
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.api_route("/mcp", methods=["POST", "GET", "DELETE"])
    async def mcp(request: Request) -> Response:
        incoming = request.headers.getlist("origin")
        if incoming and (len(incoming) != 1 or incoming[0] not in origins):
            raise HTTPException(403, "Origin is not allowed")
        try:
            credentials = registry.current()
        except (ValueError, OSError, RecursionError):
            raise HTTPException(503, "Credential registry unavailable") from None
        principal = _authorize(request, credentials)
        now = time.monotonic()
        for key, session in list(sessions.items()):
            # Reclaim idle sessions and those whose credential was revoked or changed.
            stale = session.expires_at <= now or session.principal not in credentials
            if stale and not session.lock.locked():
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
            if (
                sum(1 for item in sessions.values() if item.actor == principal.actor)
                >= max_sessions_per_principal
            ):
                raise HTTPException(503, "Too many active MCP sessions for this credential")
            server = _new_server(
                store, principal, extended=extended, summarizer=summarizer, guard=guard
            )
            initialized = server.handle(message)
            if initialized is None:
                return Response(status_code=202)
            if server.version is None:
                return JSONResponse(initialized)
            session_id = uuid4().hex
            sessions[session_id] = Session(
                principal.actor, server, time.monotonic() + IDLE_SECONDS, principal
            )
            return JSONResponse(initialized, headers={SESSION_HEADER: session_id})

        active = sessions.get(session_id)
        if active is None or active.actor != principal.actor or active.principal != principal:
            raise HTTPException(404, "MCP session not found")
        protocol = request.headers.get(VERSION_HEADER)
        if protocol is not None and protocol != active.server.version:
            raise HTTPException(400, "MCP protocol version does not match the session")
        async with active.lock:
            if sessions.get(session_id) is not active:
                raise HTTPException(404, "MCP session not found")
            try:
                refreshed = _authorize(request, registry.current())
            except (ValueError, OSError, RecursionError):
                raise HTTPException(503, "Credential registry unavailable") from None
            if refreshed != principal:
                raise HTTPException(401, "Credential changed; initialize a new session")
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
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--extended-tools", action="store_true")
    parser.add_argument("--handoff-archive", type=Path)
    parser.add_argument("--handoff-pm-hub", type=Path)
    parser.add_argument("--handoff-notes", type=Path)
    parser.add_argument("--summary-url")
    parser.add_argument("--summary-model")
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
        _parse_principals(principals, args.namespace)
        _read_origins(tuple(args.origin))
        summarizer = None
        if args.summary_url or args.summary_model:
            if not args.summary_url or not args.summary_model:
                raise ValueError("Configure summary URL and model together.")
            from summaries import OllamaSummarizer

            summarizer = OllamaSummarizer(args.summary_url, args.summary_model)
        app = create_app(
            Store(
                args.db,
                args.sources,
                default_namespace=args.namespace,
                handoff_destinations=handoff_destinations(args),
            ),
            args.principals,
            allowed_origins=tuple(args.origin),
            extended=args.extended_tools,
            summarizer=summarizer,
        )
    except (ValueError, OSError, sqlite3.Error, RecursionError):
        print("Memory Hall HTTP startup failed: check paths and configuration", file=sys.stderr)
        return 1
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
