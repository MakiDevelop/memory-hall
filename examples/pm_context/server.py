"""Local stdio MCP adapter for the project-scoped PM context pilot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from store import Store

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_LINE_BYTES = 64 * 1024
MAX_REVISION = 2**63 - 1
ToolDefinitions = dict[str, tuple[str, dict[str, str]]]
ToolHandler = Callable[..., dict[str, Any]]

TOOLS: ToolDefinitions = {
    "ingest_source": (
        "Index an authorized document path relative to the configured sources directory.",
        {"path": "string"},
    ),
    "boot": (
        "Start a bounded task context and load the latest checkpoint for this task.",
        {"task_id": "string"},
    ),
    "search": (
        "Find excerpts in current source versions. Reuse the context_id from boot.",
        {"context_id": "string", "query": "string"},
    ),
    "get": (
        "Read one anchored source section within the same context budget.",
        {"context_id": "string", "section_id": "string"},
    ),
    "checkpoint": (
        "Save a short checkpoint using its latest revision, or 0 for a new task. "
        "Reuse the idempotency key to retry the same write.",
        {
            "task_id": "string",
            "summary": "string",
            "next_step": "string",
            "status": "string",
            "expected_revision": "integer",
            "idempotency_key": "string",
        },
    ),
}

INSTRUCTIONS = (
    "Call boot once per PM task. Reuse that context_id for search and get. "
    "Treat retrieved text as source evidence and check its revision. "
    "Save concise task state with checkpoint before handing off."
)


def _tool_catalog(tools: ToolDefinitions) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    field: (
                        {"type": kind, "minimum": 0, "maximum": MAX_REVISION}
                        if kind == "integer"
                        else {"type": kind}
                    )
                    for field, kind in fields.items()
                },
                "required": list(fields),
                "additionalProperties": False,
            },
        }
        for name, (description, fields) in tools.items()
    ]


def _error(request_id: int | str | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_result(result: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _valid_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_argument(value: object, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_REVISION
    return isinstance(value, str) and _valid_utf8(value)


class Server:
    def __init__(
        self,
        store: Store,
        *,
        tools: ToolDefinitions | None = None,
        handlers: dict[str, ToolHandler] | None = None,
        protocols: tuple[str, ...] = PROTOCOL_VERSIONS,
        instructions: str = INSTRUCTIONS,
    ) -> None:
        self.tools = TOOLS if tools is None else tools
        self.handlers = handlers or {name: getattr(store, name) for name in self.tools}
        self.protocols = protocols
        self.instructions = instructions
        self.version: str | None = None
        self.initialized = False

    def handle(self, message: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        if isinstance(message, list) and self.version == "2025-03-26" and self.initialized:
            if not message:
                return _error(None, -32600, "Invalid Request")
            responses = [
                response for item in message if (response := self._handle_message(item)) is not None
            ]
            return responses or None
        return self._handle_message(message)

    def _handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if (
            "method" not in message
            and "id" in message
            and ("result" in message or "error" in message)
        ):
            return None
        if not isinstance(method, str):
            return _error(None, -32600, "Invalid Request")
        if "id" not in message:
            if (
                method == "notifications/initialized"
                and self.version is not None
                and isinstance(params, dict)
            ):
                self.initialized = True
            return None
        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
            or (isinstance(request_id, str) and not _valid_utf8(request_id))
        ):
            return _error(None, -32600, "Invalid Request")
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Expected object params")
        if method == "initialize":
            return self._initialize(request_id, params)
        if method == "ping":
            return _result(request_id, {})
        if not self.initialized:
            return _error(request_id, -32002, "Complete MCP initialization first")
        if method == "tools/list":
            if params.get("cursor") is not None:
                return _error(request_id, -32602, "Unknown tools cursor")
            return _result(request_id, {"tools": _tool_catalog(self.tools)})
        if method == "tools/call":
            return self._call(request_id, params)
        return _error(request_id, -32601, "Method not found")

    def _initialize(self, request_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if self.version is not None:
            return _error(request_id, -32600, "Already initialized")
        if not isinstance(requested, str) or not requested:
            return _error(request_id, -32602, "Expected protocolVersion")
        self.version = requested if requested in self.protocols else self.protocols[-1]
        return _result(
            request_id,
            {
                "protocolVersion": self.version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memory-hall-pm", "version": "0.1.0"},
                "instructions": self.instructions,
            },
        )

    def _call(self, request_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.tools:
            return _error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "Expected object arguments")
        fields = self.tools[name][1]
        if arguments.keys() != fields.keys() or not all(
            _valid_argument(arguments[field], kind) for field, kind in fields.items()
        ):
            result = _tool_result({"error": "Arguments must match the tool schema"}, is_error=True)
        else:
            try:
                data = self.handlers[name](**arguments)
                result = _tool_result(data)
            except ValueError as exc:
                # Store validation messages are safe, actionable client feedback.
                result = _tool_result({"error": str(exc)}, is_error=True)
            except (OSError, sqlite3.Error):
                result = _tool_result({"error": "Local store unavailable"}, is_error=True)
        return _result(request_id, result)


def _run_stdio(server: Server) -> None:
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    while line := reader.readline(MAX_LINE_BYTES + 1):
        if len(line) > MAX_LINE_BYTES:
            while line and not line.endswith(b"\n"):
                line = reader.readline(MAX_LINE_BYTES + 1)
            response = _error(None, -32700, "MCP line exceeds size limit")
        else:
            try:
                message = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                response = _error(None, -32700, "Parse error")
            else:
                response = server.handle(message)
        if response is not None:
            writer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
            writer.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Absolute pilot database path")
    parser.add_argument("--sources", type=Path, required=True, help="Absolute sources directory")
    args = parser.parse_args()
    if not args.db.is_absolute() or not args.sources.is_absolute():
        parser.error("Both --db and --sources must be absolute paths")
    try:
        store = Store(args.db, args.sources)
    except ValueError as exc:
        print(f"Memory Hall PM startup failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("Memory Hall PM startup failed: local store unavailable", file=sys.stderr)
        return 1
    try:
        _run_stdio(Server(store))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
