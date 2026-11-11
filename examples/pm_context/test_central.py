"""Exercise the central MCP adapter with independent synthetic clients."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None

from store import Store
from upload import upload

VERSION = "2025-11-25"
HEADERS = {"Accept": "application/json, text/event-stream"}
TOKENS = {
    "claude": "synthetic-writer-claude-only-for-fixture",
    "codex": "synthetic-writer-codex-only-for-fixture",
    "review": "synthetic-reader-review-only-for-fixture",
}


@unittest.skipIf(TestClient is None, "Run with the repository dependencies for HTTP tests")
class CentralTests(unittest.TestCase):
    def setUp(self):
        from http_server import create_app

        root = Path(tempfile.mkdtemp(prefix="mh31-http-test-"))
        sources = root / "sources"
        sources.mkdir()
        principals = {
            name: {
                "token_sha256": hashlib.sha256(value.encode()).hexdigest(),
                "role": "read" if name == "review" else "write",
            }
            for name, value in TOKENS.items()
        }
        store = Store(root / "pilot.sqlite3", sources)
        self.client = TestClient(create_app(store, principals))

    def post(self, method, headers, params=None, request_id=1):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            payload["id"] = request_id
        return self.client.post("/mcp", headers=headers, json=payload)

    def connect(self, name):
        headers = {**HEADERS, "Authorization": f"Bearer {TOKENS[name]}"}
        response = self.post(
            "initialize",
            headers,
            {
                "protocolVersion": VERSION,
                "capabilities": {},
                "clientInfo": {"name": name, "version": "1"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["protocolVersion"], VERSION)
        headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        headers["MCP-Protocol-Version"] = VERSION
        self.assertEqual(
            self.post("notifications/initialized", headers, request_id=None).status_code, 202
        )
        return headers

    def call(self, headers, name, **arguments):
        response = self.post("tools/call", headers, {"name": name, "arguments": arguments})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def test_two_writers_share_documents_and_checkpoints(self):
        claude = self.connect("claude")
        codex = self.connect("codex")
        upload = {
            "path": "spec/export.md",
            "text": "# 匯出\n規格 timeout 41 秒。",
            "expected_sha256": "",
            "idempotency_key": "upload-1",
        }
        first = self.call(claude, "ingest_text", **upload)
        self.assertEqual(first["actor"], "claude")
        self.assertEqual(self.call(claude, "ingest_text", **upload), first)
        source = self.call(codex, "source_status", path=upload["path"])
        self.assertEqual(source["sha256"], first["sha256"])
        boot = self.call(codex, "boot", task_id="EXPORT")
        context = boot["context_id"]
        hit = self.call(codex, "search", context_id=context, query="匯出")["results"][0]
        section = self.call(codex, "get", context_id=context, section_id=hit["section_id"])
        self.assertIn("41", section["section"]["text"])
        state = self.call(
            codex,
            "checkpoint",
            task_id="EXPORT",
            summary="核對規格",
            next_step="驗證重試",
            status="open",
            expected_revision=0,
            idempotency_key="checkpoint-1",
        )
        self.assertEqual(state["actor"], "codex")
        self.assertEqual(self.call(claude, "boot", task_id="EXPORT")["revision"], 1)
        updated = self.call(
            codex,
            "ingest_text",
            **{
                **upload,
                "text": "# 匯出\n規格 timeout 21 秒。",
                "expected_sha256": source["sha256"],
            },
        )
        self.assertEqual(updated["actor"], "codex")
        self.assertEqual(self.call(claude, "ingest_text", **upload), first)
        self.assertEqual(
            self.call(claude, "source_status", path=upload["path"])["sha256"], updated["sha256"]
        )
        self.assertEqual(
            self.call(codex, "search", context_id=context, query="匯出")["status"], "context_stale"
        )

    def test_permissions_and_session_binding(self):
        unauthenticated = self.post("tools/list", HEADERS)
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertIn("Bearer", unauthenticated.headers["WWW-Authenticate"])
        reader = self.connect("review")
        writer = self.connect("claude")
        catalog = self.post("tools/list", reader).json()["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in catalog}, {"boot", "search", "get", "source_status"}
        )
        denied = self.post("tools/call", reader, {"name": "checkpoint", "arguments": {}})
        self.assertTrue(
            denied.status_code == 403
            or "error" in denied.json()
            or denied.json()["result"].get("isError")
        )
        wrong_session = {**writer, "Mcp-Session-Id": reader["Mcp-Session-Id"]}
        self.assertEqual(self.post("tools/list", wrong_session).status_code, 404)
        origin = {**writer, "Origin": "https://unknown.invalid"}
        self.assertEqual(self.post("tools/list", origin).status_code, 403)

    def test_file_uploader_uses_the_same_mcp_entry(self):
        def open_request(request, timeout):
            self.assertEqual(timeout, 30)
            response = self.client.post(
                urlsplit(request.full_url).path,
                headers=dict(request.header_items()),
                content=request.data,
            )
            body = io.BytesIO(response.content)
            body.status = response.status_code
            body.headers = response.headers
            return body

        with patch("upload.build_opener") as opener:
            opener.return_value.open.side_effect = open_request
            receipt = upload(
                "http://127.0.0.1:8766/mcp",
                TOKENS["claude"],
                "manual.md",
                "# 合成\n驗證成功。",
                "",
                "direct-upload",
            )
        self.assertEqual(receipt["actor"], "claude")
        reviewer = self.connect("review")
        self.assertEqual(
            self.call(reviewer, "source_status", path="manual.md")["sha256"],
            receipt["sha256"],
        )
