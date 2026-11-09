"""Plain-text PM MCP client, shared by all agent vendors."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PROTOCOL = "2025-11-25"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024


class Client:
    def __init__(self, url: str, token: str):
        try:
            target = urlsplit(url)
            _ = target.port
        except ValueError:
            raise ValueError("Use a valid HTTPS MCP URL or explicit loopback HTTP.") from None
        local = target.scheme == "http" and target.hostname in ("127.0.0.1", "localhost", "::1")
        if (
            any(char.isspace() for char in url)
            or not target.hostname
            or (target.scheme != "https" and not local)
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
        ):
            raise ValueError("Use an HTTPS MCP URL or explicit loopback HTTP.")
        if not token or len(token) > 1024 or any(char.isspace() for char in token):
            raise ValueError("Set a valid MEMORY_HALL_31_TOKEN through the credential manager.")
        self.url = url
        self.http = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        )
        self.request_id = 0

    def __enter__(self):
        try:
            response = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "mh-pm-cli", "version": "1"},
                },
            )
            session = response.headers.get("Mcp-Session-Id")
            payload = response.json().get("result", {})
            if not session or payload.get("protocolVersion") != PROTOCOL:
                raise ValueError("The server did not establish a compatible MCP session.")
            self.http.headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL})
            self.request("notifications/initialized", {}, notification=True)
            return self
        except Exception:
            self.http.close()
            raise

    def __exit__(self, *_):
        try:
            self.http.delete(self.url, timeout=1)
        except httpx.RequestError:
            pass
        self.http.close()

    def request(self, method: str, params: dict, *, notification: bool = False) -> httpx.Response:
        self.request_id += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.request_id
        try:
            with self.http.stream("POST", self.url, json=body) as incoming:
                if incoming.status_code not in (200, 202):
                    raise ValueError(f"MCP endpoint returned HTTP {incoming.status_code}.")
                data = bytearray()
                for chunk in incoming.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ValueError("MCP response exceeds the bounded client limit.")
                response = httpx.Response(
                    incoming.status_code, headers=incoming.headers, content=bytes(data)
                )
        except httpx.RequestError:
            raise ValueError(
                "MCP connection failed or timed out; preserve the original write key."
            ) from None
        if notification:
            if response.status_code != 202:
                raise ValueError("MCP initialization was not acknowledged.")
        else:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("id") != self.request_id:
                raise ValueError("Invalid MCP response identity.")
            if "error" in payload:
                raise ValueError("MCP request rejected by the server.")
        return response

    def call(self, name: str, arguments: dict) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response.json()["result"]
        payload = json.loads(result["content"][0]["text"])
        if result.get("isError"):
            raise ValueError(payload.get("error", "MCP tool failed."))
        if not isinstance(payload, dict):
            raise ValueError("MCP tool returned an invalid result.")
        return payload


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mh pm", description=__doc__)
    root.add_argument("--url", default=os.environ.get("MEMORY_HALL_31_URL"))
    root.add_argument("--namespace")
    root.add_argument("--budget-bytes", type=int)
    commands = root.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("boot")
    boot.add_argument("task_id", nargs="?", default="project")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--type")
    search.add_argument("--status", default="active")
    search.add_argument("--detail", choices=("summary", "full"), default="summary")
    search.add_argument("--limit", type=int, default=6)
    search.add_argument("--include-history", action="store_true")
    get = commands.add_parser("get")
    get.add_argument("section_id")
    digest = commands.add_parser("digest")
    digest.add_argument("--since", required=True)
    digest.add_argument("--until", required=True)
    for child in (search, get, digest):
        child.add_argument("--context-id")
        child.add_argument("--task-id", default="project")
    source = commands.add_parser("source-status")
    source.add_argument("path")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("path")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--expected-sha256", default="")
    ingest.add_argument("--expected-metadata-revision", type=int)
    ingest.add_argument("--request-id", default="")
    ingest.add_argument("--event-date")
    ingest.add_argument("--type", dest="record_type", default="document")
    ingest.add_argument("--summary")
    ingest.add_argument(
        "--summary-mode", choices=("extractive", "manual", "model"), default="extractive"
    )
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("task_id")
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--next-step", required=True)
    checkpoint.add_argument("--status", choices=("open", "blocked", "done"), required=True)
    checkpoint.add_argument("--expected-revision", type=int, required=True)
    checkpoint.add_argument("--request-id", default="")
    handoff = commands.add_parser("handoff")
    actions = handoff.add_subparsers(dest="action", required=True)
    actions.add_parser("template")
    read = actions.add_parser("read")
    read.add_argument("line_id")
    read.add_argument("--context-id")
    read.add_argument("--task-id", default="project")
    write = actions.add_parser("write")
    write.add_argument("line_id")
    write.add_argument("file", type=Path)
    write.add_argument("--expected-revision", type=int, required=True)
    write.add_argument("--request-id", default="")
    write.add_argument("--event-date")
    for action in ("status", "retry"):
        child = actions.add_parser(action)
        child.add_argument("operation_id")
    return root


def _read_file(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("The input file exceeds 2 MiB.")
    return data.decode("utf-8")


def _arguments(args: argparse.Namespace, client: Client) -> tuple[str, dict]:
    scope = {"namespace": args.namespace} if args.namespace is not None else {}
    read_scope = {
        **scope,
        **({"budget_bytes": args.budget_bytes} if args.budget_bytes is not None else {}),
    }
    command = args.command
    if command == "boot":
        return "boot", {"task_id": args.task_id, **read_scope}
    if command in ("search", "get", "digest") or (command == "handoff" and args.action == "read"):
        context_id = args.context_id
        if not context_id:
            boot = client.call("boot", {"task_id": args.task_id, **scope})
            if boot.get("status") != "ok":
                raise ValueError("Could not start a bounded task context.")
            context_id = boot["context_id"]
        values = {"context_id": context_id, **read_scope}
        if command == "search":
            values.update(
                {
                    key: getattr(args, key)
                    for key in (
                        "query",
                        "since",
                        "until",
                        "type",
                        "status",
                        "detail",
                        "limit",
                        "include_history",
                    )
                    if getattr(args, key) is not None
                }
            )
        elif command == "get":
            values["section_id"] = args.section_id
        elif command == "digest":
            values.update(since=args.since, until=args.until)
        else:
            values["line_id"] = args.line_id
        return "handoff_read" if command == "handoff" else command, values
    if command == "source-status":
        return "source_status", {"path": args.path, **scope}
    if command == "handoff" and args.action == "template":
        return "handoff_template", {}
    if command == "handoff" and args.action in ("status", "retry"):
        return f"handoff_{args.action}", {"operation_id": args.operation_id, **scope}
    key = args.request_id or uuid.uuid4().hex
    if not args.request_id:
        print(f"request_id={key}", file=sys.stderr, flush=True)
    if command == "checkpoint":
        return command, {
            **scope,
            "task_id": args.task_id,
            "summary": args.summary,
            "next_step": args.next_step,
            "status": args.status,
            "expected_revision": args.expected_revision,
            "idempotency_key": key,
        }
    if command == "ingest":
        values = {
            **scope,
            "path": args.path,
            "text": _read_file(args.file),
            "expected_sha256": args.expected_sha256,
            "idempotency_key": key,
            "record_type": args.record_type,
            "summary_mode": args.summary_mode,
        }
        if args.summary is not None:
            values["summary"] = args.summary
        if args.event_date is not None:
            values["event_date"] = args.event_date
        if args.expected_metadata_revision is not None:
            values["expected_metadata_revision"] = args.expected_metadata_revision
        return "ingest_text", values
    values = {
        **scope,
        "line_id": args.line_id,
        "body": _read_file(args.file),
        "expected_revision": args.expected_revision,
        "idempotency_key": key,
    }
    if args.event_date is not None:
        values["event_date"] = args.event_date
    return "handoff_write", values


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if not args.url:
        root.error("Set MEMORY_HALL_31_URL or pass --url.")
    try:
        with Client(args.url, os.environ.get("MEMORY_HALL_31_TOKEN", "")) as client:
            name, arguments = _arguments(args, client)
            result = client.call(name, arguments)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") in (
            "context_stale",
            "budget_exhausted",
            "budget_too_small",
            "error",
        ):
            return 1
        return 0 if result.get("state") in (None, "published", "complete") else 2
    except (ValueError, OSError, KeyError, IndexError, TypeError):
        # ValueErrors produced by this client and server are safe diagnostics.
        error = sys.exc_info()[1]
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            print(str(error), file=sys.stderr)
        else:
            print("Could not read input or understand the MCP response.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
