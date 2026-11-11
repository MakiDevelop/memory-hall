"""Local stdio MCP adapter for the project-scoped PM context pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from store import Store

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_LINE_BYTES = 64 * 1024
MAX_REVISION = 2**63 - 1
ToolDefinitions = dict[str, tuple[str, dict[str, str]]]
ToolHandler = Callable[..., dict[str, Any]]

METADATA_FIELDS = {
    "namespace": "string?",
    "summary": "string?",
    "record_type": "string?",
    "event_date": "string?",
    "freshness": "string?",
    "lifecycle": "string?",
    "expected_metadata_revision": "integer?",
}

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


OPTIONAL_FIELDS = {
    "boot": {"namespace": "string?", "budget_bytes": "integer?"},
    "search": {
        "namespace": "string?",
        "budget_bytes": "integer?",
        "since": "string?",
        "until": "string?",
        "type": "string?",
        "status": "string?",
        "detail": "string?",
        "limit": "integer?",
        "include_history": "boolean?",
    },
    "get": {"namespace": "string?", "budget_bytes": "integer?"},
    "checkpoint": {"namespace": "string?"},
    "ingest_source": METADATA_FIELDS,
}
TOOLS = {
    name: (description, {**fields, **OPTIONAL_FIELDS.get(name, {})})
    for name, (description, fields) in TOOLS.items()
}

EXTENDED_TOOLS: ToolDefinitions = {
    "digest": (
        "List dated source changes in the current namespace and context budget.",
        {
            "context_id": "string",
            "since": "string",
            "until": "string",
            "namespace": "string?",
            "budget_bytes": "integer?",
        },
    ),
    "precheck": (
        "Scan text and metadata for sensitive content before an upload. Stores nothing, calls "
        "no model, and grants no exceptions; returns the source SHA-256, rule version and "
        "field/category/line locations.",
        {"text": "string", "path": "string?", **METADATA_FIELDS},
    ),
    "handoff_template": ("Get the fixed eight-section handoff template.", {}),
    "handoff_write": (
        "Publish a validated handoff with idempotency, revision and delivery receipts.",
        {
            "line_id": "string",
            "body": "string",
            "expected_revision": "integer",
            "idempotency_key": "string",
            "namespace": "string?",
            "event_date": "string?",
        },
    ),
    "handoff_read": (
        "Read the last completely published handoff in the same context budget.",
        {
            "context_id": "string",
            "line_id": "string",
            "namespace": "string?",
            "budget_bytes": "integer?",
        },
    ),
    "handoff_status": (
        "Get bounded delivery state for an accepted handoff operation.",
        {"operation_id": "string", "namespace": "string?"},
    ),
    "handoff_retry": (
        "Resume incomplete handoff destinations without changing the original submission.",
        {"operation_id": "string", "namespace": "string?"},
    ),
}


INSTRUCTIONS = (
    "Call boot once per PM task. Reuse that context_id for search and get. "
    "Treat retrieved text as source evidence and check its revision. "
    "Save concise task state with checkpoint before handing off."
)


class RepeatGuard:
    """Count identical tool-call schema errors per identity so a client stops looping.

    Keys never include argument values. A successful call of the same tool by the same
    identity clears its counters; reconnecting does not, because state lives here and not
    in the MCP session.
    """

    def __init__(
        self,
        threshold: int = 3,
        window_seconds: float = 600.0,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.window = window_seconds
        self.max_keys = max_keys
        self.clock = clock
        self._counts: dict[tuple, list[float]] = {}

    def record(self, key: tuple) -> int:
        """Count one error for a bounded key; least recently seen keys are evicted first."""
        now = self.clock()
        entry = self._counts.pop(key, None)
        if entry is not None and now - entry[1] > self.window:
            entry = None
        # Insertion order equals last-seen order, so expired keys sit at the front.
        while self._counts:
            oldest = next(iter(self._counts))
            if now - self._counts[oldest][1] <= self.window and len(self._counts) < self.max_keys:
                break
            del self._counts[oldest]
        entry = entry or [0, now]
        entry[0] += 1
        entry[1] = now
        self._counts[key] = entry
        return int(entry[0])

    def clear(self, actor: str, tool: str) -> None:
        for key in [k for k in self._counts if k[0] == actor and k[2] == tool]:
            del self._counts[key]


def _digest(value: str) -> str:
    """Fixed-size key part so caller-controlled names cannot grow guard memory."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:32]


def _example(fields: dict[str, str]) -> dict[str, Any]:
    samples = {"integer": 0, "boolean": False}
    return {
        field: samples.get(kind, "...") for field, kind in fields.items() if not kind.endswith("?")
    }


def _schema_problem(fields: dict[str, str], arguments: dict[str, Any]) -> dict[str, Any]:
    required = {field for field, kind in fields.items() if not kind.endswith("?")}
    wrong = {
        field: f"expected {fields[field].rstrip('?')}"
        for field, value in arguments.items()
        if field in fields and not _valid_argument(value, fields[field])
    }
    problem: dict[str, Any] = {}
    if required - arguments.keys():
        problem["missing"] = sorted(required - arguments.keys())
    if arguments.keys() - fields.keys():
        # Caller-chosen names: bound the count and length echoed back.
        unknown = sorted(arguments.keys() - fields.keys())
        problem["unknown"] = [name[:64] for name in unknown[:20]]
        if len(unknown) > 20:
            problem["unknown_more"] = len(unknown) - 20
    if wrong:
        problem["wrong_type"] = dict(sorted(wrong.items()))
    return problem


def _tool_catalog(tools: ToolDefinitions) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    field: (
                        {"type": "integer", "minimum": 0, "maximum": MAX_REVISION}
                        if kind.rstrip("?") == "integer"
                        else {"type": kind.rstrip("?")}
                    )
                    for field, kind in fields.items()
                },
                "required": [field for field, kind in fields.items() if not kind.endswith("?")],
                "additionalProperties": False,
            },
        }
        for name, (description, fields) in tools.items()
    ]


def _error(
    request_id: int | str | None, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


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
    kind = kind.rstrip("?")
    if kind == "integer":
        return type(value) is int and 0 <= value <= MAX_REVISION
    if kind == "boolean":
        return type(value) is bool
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
        guard: RepeatGuard | None = None,
        actor: str = "local",
    ) -> None:
        self.guard = guard or RepeatGuard()
        self.actor = actor
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
            or (isinstance(request_id, int) and not -MAX_REVISION <= request_id <= MAX_REVISION)
            or (
                isinstance(request_id, str)
                and (not _valid_utf8(request_id) or len(request_id.encode("utf-8")) > 256)
            )
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
            label = name if isinstance(name, str) else type(name).__name__
            count = self.guard.record((self.actor, None, "?unknown", _digest(label)))
            hint = f"; available tools: {', '.join(sorted(self.tools))}"
            stopped = count >= self.guard.threshold
            return _error(
                request_id,
                -32602,
                "Unknown tool" + hint,
                {"attempt": count, "stop_retrying": stopped},
            )
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            count = self.guard.record((self.actor, None, name, "non-object-arguments"))
            return _error(
                request_id,
                -32602,
                "Expected object arguments",
                {"attempt": count, "stop_retrying": count >= self.guard.threshold},
            )
        fields = self.tools[name][1]
        required = {field for field, kind in fields.items() if not kind.endswith("?")}
        if not required <= arguments.keys() <= fields.keys() or not all(
            _valid_argument(value, fields[field]) for field, value in arguments.items()
        ):
            problem = _schema_problem(fields, arguments)
            namespace = arguments.get("namespace")
            fingerprint = _digest(json.dumps(problem, sort_keys=True, ensure_ascii=False))
            scope = _digest(namespace) if isinstance(namespace, str) else None
            count = self.guard.record((self.actor, scope, name, fingerprint))
            body: dict[str, Any] = {"error": "Arguments must match the tool schema", **problem}
            body["example"] = _example(fields)
            body["attempt"] = count
            if count >= self.guard.threshold:
                body["stop_retrying"] = True
                body["reason"] = (
                    f"The same invalid call failed {count} times. Fix the arguments using "
                    "missing/unknown/wrong_type and example, or ask a person; do not retry as is."
                )
            result = _tool_result(body, is_error=True)
        else:
            try:
                data = self.handlers[name](**arguments)
                self.guard.clear(self.actor, name)
                result = _tool_result(data)
            except ValueError as exc:
                # Store validation messages are safe, actionable client feedback.
                message = str(exc).encode("utf-8")[:384].decode("utf-8", errors="ignore")
                result = _tool_result({"error": message}, is_error=True)
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


def handoff_destinations(args: argparse.Namespace) -> dict[str, Path] | None:
    paths = {
        "archive": args.handoff_archive,
        "pm_hub": args.handoff_pm_hub,
        "notes": args.handoff_notes,
    }
    if all(path is None for path in paths.values()):
        return None
    if any(path is None or not path.is_absolute() for path in paths.values()):
        raise ValueError("Configure all three absolute handoff destination directories.")
    if len({path.resolve() for path in paths.values()}) != 3:
        raise ValueError("Handoff destinations must be distinct directories.")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Absolute pilot database path")
    parser.add_argument("--sources", type=Path, required=True, help="Absolute sources directory")
    parser.add_argument("--namespace", default="default", help="Explicit legacy/default namespace")
    parser.add_argument("--extended-tools", action="store_true", help="Enable batch-one tools")
    parser.add_argument("--handoff-archive", type=Path)
    parser.add_argument("--handoff-pm-hub", type=Path)
    parser.add_argument("--handoff-notes", type=Path)
    args = parser.parse_args()
    if not args.db.is_absolute() or not args.sources.is_absolute():
        parser.error("Both --db and --sources must be absolute paths")
    try:
        destinations = handoff_destinations(args)
        store = Store(
            args.db,
            args.sources,
            default_namespace=args.namespace,
            handoff_destinations=destinations,
        )
    except ValueError as exc:
        print(f"Memory Hall PM startup failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("Memory Hall PM startup failed: local store unavailable", file=sys.stderr)
        return 1
    try:
        tools = {**TOOLS, **EXTENDED_TOOLS} if args.extended_tools else TOOLS
        handlers = {
            name: getattr(store, name)
            for name in tools
            if name not in ("handoff_template", "precheck")
        }
        if "precheck" in tools:
            from safety import precheck

            handlers["precheck"] = lambda **arguments: precheck(arguments)
        if "handoff_template" in tools:
            from handoff_template import template

            handlers["handoff_template"] = lambda: {"status": "ok", "body": template()}
        _run_stdio(Server(store, tools=tools, handlers=handlers))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
