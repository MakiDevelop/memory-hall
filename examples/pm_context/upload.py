"""Send a UTF-8 document to the central PM store without a model call."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROTOCOL_VERSION = "2025-11-25"
MAX_FILE_BYTES = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def upload(
    url: str, token: str, path: str, text: str, expected_sha256: str, request_key: str
) -> dict:
    target = urlsplit(url)
    local = target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not target.hostname
        or (target.scheme != "https" and not local)
        or target.username is not None
        or target.password is not None
        or target.fragment
        or target.query
    ):
        raise ValueError("Use an HTTPS MCP URL or a local loopback test URL.")

    opener = build_opener(ProxyHandler({}), NoRedirect())
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def send(method: str, params: dict, request_id: int | None = None):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # The URL is restricted to HTTPS or an explicit loopback test above.
        request = Request(url, data=encoded, headers=headers)  # noqa: S310
        try:
            with opener.open(request, timeout=30) as response:
                if request_id is None:
                    if response.status != 202:
                        raise ValueError("The server did not acknowledge MCP initialization.")
                    return {}, response.headers
                payload = json.loads(response.read(65537))
                if not isinstance(payload, dict) or payload.get("id") != request_id:
                    raise ValueError("Invalid MCP response.")
                if "result" not in payload:
                    raise ValueError("The server rejected the MCP request.")
                return payload["result"], response.headers
        except HTTPError as error:
            raise ValueError(f"MCP endpoint returned HTTP {error.code}.") from None
        except (URLError, TimeoutError):
            raise ValueError(
                "Could not confirm the MCP request. Retry with the same key."
            ) from None

    initial, response_headers = send(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "memory-hall-file-upload", "version": "0.1.0"},
        },
        1,
    )
    session = response_headers.get("Mcp-Session-Id")
    if initial.get("protocolVersion") != PROTOCOL_VERSION or not session:
        raise ValueError("The endpoint did not establish the expected MCP session.")
    headers["Mcp-Session-Id"] = session
    headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
    send("notifications/initialized", {})
    result, _ = send(
        "tools/call",
        {
            "name": "ingest_text",
            "arguments": {
                "path": path,
                "text": text,
                "expected_sha256": expected_sha256,
                "idempotency_key": request_key,
            },
        },
        2,
    )
    payload = json.loads(result["content"][0]["text"])
    if result.get("isError"):
        raise ValueError(payload.get("error", "The server rejected the upload."))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Central MCP URL")
    parser.add_argument("path", help="Project-relative logical document name")
    parser.add_argument("file", type=Path, help="Local UTF-8 Markdown or text file")
    parser.add_argument("--expect", default="", help="Current source SHA-256, omit to create only")
    parser.add_argument("--request-id", default="", help="Reuse the original key after timeout")
    args = parser.parse_args()
    token = os.environ.get("MEMORY_HALL_31_TOKEN")
    if not token:
        parser.error("Set MEMORY_HALL_31_TOKEN using the project's credential manager.")
    request_key = args.request_id or uuid.uuid4().hex
    try:
        with args.file.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Document exceeds the 2 MiB limit.")
        text = raw.decode("utf-8")
        print(f"request_id={request_key}", file=sys.stderr, flush=True)
        result = upload(args.url, token, args.path, text, args.expect, request_key)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        if isinstance(error, (OSError, KeyError, IndexError, TypeError)):
            print("Could not read the file or understand the MCP response.", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
