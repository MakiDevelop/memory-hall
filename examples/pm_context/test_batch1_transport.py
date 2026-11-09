"""Real SQLite, TCP and CLI regression checks for the PM service boundary."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from budget import BudgetTooSmall, compact_bytes, finalize
from handoff_template import template
from http_server import Principal, _invoke
from store import Store

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PYTHON = sys.executable
PROTOCOL = "2025-11-25"


class BudgetTests(unittest.TestCase):
    def test_serialized_utf8_bytes_and_conservative_charge(self):
        for ceiling in (1024, 4096, 8000):
            with self.subTest(ceiling=ceiling):
                payload = {"status": "ok", "text": "中文🙂" * 30}
                result, charged = finalize(payload, ceiling, 8000)
                self.assertEqual(result["used_bytes"], len(compact_bytes(result)))
                self.assertLessEqual(result["used_bytes"], ceiling)
                self.assertLessEqual(result["used_bytes"], charged)
                self.assertEqual(result["remaining_context_bytes"], 8000 - charged)
                self.assertEqual(result["text"], payload["text"])

    def test_repeated_charges_and_digit_boundaries(self):
        for starting in (999, 1000, 1001, 4096, 8000):
            remaining = starting
            charged_total = 0
            for _ in range(60):
                try:
                    result, charged = finalize({"status": "ok", "text": "資料"}, 1024, remaining)
                except BudgetTooSmall:
                    break
                charged_total += charged
                remaining = result["remaining_context_bytes"]
                self.assertEqual(result["used_bytes"], len(compact_bytes(result)))
                self.assertEqual(remaining + charged_total, starting)
            self.assertGreater(charged_total, 0)

    def test_invalid_and_too_small_budgets(self):
        for value in (True, 0, -1, 8001, "1024"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finalize({"status": "ok"}, value, 8000)
        with self.assertRaises(BudgetTooSmall):
            finalize({"status": "ok"}, 1, 8000)


class HandlerModelTests(unittest.TestCase):
    def test_raw_request_replay_and_metadata_scan_precede_model(self):
        root = Path(tempfile.mkdtemp(prefix="mh31-model-handler-"))
        sources = root / "sources"
        sources.mkdir()
        store = Store(root / "pilot.sqlite3", sources)
        principal = Principal("model-writer", "0" * 64, "agent_writer")

        class Model:
            calls = 0

            def summarize(self, text):
                self.calls += 1
                if self.calls > 1:
                    raise ValueError("Synthetic offline model")
                return {
                    "summary": "合成模型摘要",
                    "summary_kind": "model",
                    "summary_model": "fixture",
                    "prompt_sha256": "1" * 64,
                }

        model = Model()
        arguments = {
            "path": "model.md",
            "text": "# 合成模型\n固定本文。",
            "expected_sha256": "",
            "idempotency_key": "model-replay",
            "summary_mode": "model",
        }
        first = _invoke(store, principal, "ingest_text", model, **arguments)
        again = _invoke(store, principal, "ingest_text", model, **arguments)
        self.assertEqual(first, again)
        self.assertEqual(model.calls, 1)
        cached = _invoke(store, principal, "ingest_text", None, **arguments)
        self.assertEqual(first, cached)
        unsafe = {**arguments, "summary": '"password": "fixture-pass"'}
        with self.assertRaises(ValueError):
            _invoke(store, principal, "ingest_text", model, **unsafe)
        self.assertEqual(model.calls, 1)


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="mh31-network-"))
        cls.sources = cls.root / "sources"
        cls.sources.mkdir()
        cls.destinations = {}
        for name in ("archive", "pm_hub", "notes"):
            path = cls.root / name
            path.mkdir()
            cls.destinations[name] = path
        cls.tokens = {
            name: hashlib.sha256(f"synthetic-{name}".encode()).hexdigest()
            for name in ("writer", "reader", "other", "pm")
        }
        cls.records = {
            name: {
                "token_sha256": hashlib.sha256(cls.tokens[name].encode()).hexdigest(),
                "role": {
                    "writer": "agent_writer",
                    "reader": "reader",
                    "other": "agent_writer",
                    "pm": "pm",
                }[name],
                "namespaces": ["project:other" if name == "other" else "project:test"],
                "default_namespace": "project:other" if name == "other" else "project:test",
            }
            for name in cls.tokens
        }
        cls.registry = cls.root / "principals.json"
        cls.registry.write_text(json.dumps(cls.records))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}/mcp"
        cls.server_log = (cls.root / "server.log").open("w+")
        command = [
            PYTHON,
            "-B",
            str(HERE / "http_server.py"),
            "--db",
            str(cls.root / "pilot.sqlite3"),
            "--sources",
            str(cls.sources),
            "--principals",
            str(cls.registry),
            "--port",
            str(port),
            "--namespace",
            "project:test",
            "--extended-tools",
            "--handoff-archive",
            str(cls.destinations["archive"]),
            "--handoff-pm-hub",
            str(cls.destinations["pm_hub"]),
            "--handoff-notes",
            str(cls.destinations["notes"]),
        ]
        # Fixed interpreter/script and isolated fixture paths; no shell or caller input.
        cls.server = subprocess.Popen(  # noqa: S603
            command,
            stdout=cls.server_log,
            stderr=subprocess.STDOUT,  # noqa: S603
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        cls.client = httpx.Client(trust_env=False, timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                cls.server_log.flush()
                raise AssertionError(f"Synthetic server exited; see {cls.root / 'server.log'}")
            try:
                if cls.client.get(cls.url).status_code == 401:
                    break
            except httpx.RequestError:
                time.sleep(0.05)
        else:
            cls.server.terminate()
            cls.server.wait(timeout=5)
            raise AssertionError("Synthetic TCP server did not become ready.")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.server_log.close()

    def connect(self, name):
        headers = {"Authorization": f"Bearer {self.tokens[name]}", "MCP-Protocol-Version": PROTOCOL}
        response = self.client.post(
            self.url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": name},
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        response = self.client.post(
            self.url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        self.assertEqual(response.status_code, 202)
        self.addCleanup(lambda: self.client.delete(self.url, headers=headers))
        return headers

    def call(self, headers, name, **arguments):
        response = self.client.post(
            self.url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def success(self, headers, name, **arguments):
        message = self.call(headers, name, **arguments)
        self.assertNotIn("error", message)
        self.assertFalse(message["result"].get("isError"), message)
        return json.loads(message["result"]["content"][0]["text"])

    def test_writer_cannot_relabel_a_stored_active_decision(self):
        pm, writer = self.connect("pm"), self.connect("writer")
        first = self.success(
            pm,
            "ingest_text",
            path="decision.md",
            text="# 決定\n保留現況。",
            expected_sha256="",
            idempotency_key="decision-pm",
            record_type="decision",
            lifecycle="active",
        )
        denied = self.call(
            writer,
            "ingest_text",
            path="decision.md",
            text="# 文件\n嘗試改類型。",
            expected_sha256=first["sha256"],
            idempotency_key="decision-relabel",
            record_type="document",
        )
        self.assertTrue(denied["result"].get("isError"), denied)
        status = self.success(pm, "source_status", path="decision.md")
        self.assertEqual(status["sha256"], first["sha256"])

    def test_actual_http_namespace_and_principal_context_scope(self):
        writer, reader, other = (self.connect(name) for name in ("writer", "reader", "other"))
        self.success(
            writer,
            "ingest_text",
            path="network.md",
            text="# 網路驗收\n已核對結果。",
            expected_sha256="",
            idempotency_key="network-1",
            event_date="2026-11-09",
        )
        boot = self.success(writer, "boot", task_id="NET")
        denied = self.call(reader, "search", context_id=boot["context_id"], query="網路")
        self.assertTrue(denied.get("error") or denied["result"].get("isError"))
        context = self.success(reader, "boot", task_id="NET")["context_id"]
        found = self.success(
            reader,
            "search",
            context_id=context,
            query="網路",
            since="2026-11-09",
            until="2026-11-09",
            budget_bytes=1024,
        )
        self.assertEqual(len(found["results"]), 1)
        self.assertLessEqual(len(compact_bytes(found)), 1024)
        denied = self.call(
            other, "get", context_id=context, section_id=found["results"][0]["section_id"]
        )
        self.assertTrue(denied.get("error") or denied["result"].get("isError"))
        denied = self.call(reader, "boot", task_id="NET", namespace="project:other")
        self.assertTrue(denied.get("error") or denied["result"].get("isError"))

    def test_actual_cli_write_and_other_reader_readback(self):
        source = self.root / "cli-source.md"
        source.write_text("# CLI驗收\n跨身分讀回資料。")
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MEMORY_HALL_31_URL": self.url,
            "MEMORY_HALL_31_TOKEN": self.tokens["writer"],
        }
        # Use the package's normal console entry through the Typer app.
        command = [PYTHON, "-B", "-c", "from memory_hall.cli.main import app; app()", "pm"]
        write = subprocess.run(  # noqa: S603
            [*command, "ingest", "cli.md", str(source), "--request-id", "cli-1"],  # noqa: S603
            env=env,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(write.returncode, 0, write.stderr)
        self.assertNotIn(self.tokens["writer"], write.stdout + write.stderr)
        env["MEMORY_HALL_31_TOKEN"] = self.tokens["reader"]
        read = subprocess.run(  # noqa: S603
            [*command, "search", "CLI驗收"],
            env=env,
            cwd=REPO,  # noqa: S603
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertTrue(json.loads(read.stdout)["results"])

    def test_handoff_tcp_three_local_readback_hashes(self):
        writer = self.connect("writer")
        published = self.success(
            writer,
            "handoff_write",
            line_id="NET-PM",
            body=template(),
            expected_revision=0,
            idempotency_key="handoff-net-1",
        )
        self.assertEqual(published["state"], "published")
        self.assertEqual(len(published["destinations"]), 3)
        for destination in published["destinations"]:
            receipt = destination["receipt"]
            raw = Path(receipt["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), published["sha256"])
        reader = self.connect("reader")
        context = self.success(reader, "boot", task_id="NET-PM")["context_id"]
        result = self.success(reader, "handoff_read", context_id=context, line_id="NET-PM")
        self.assertEqual(result["revision"], 1)
        self.assertIn("第一項", result["body"])
        self.assertLessEqual(len(compact_bytes(result)), 4096)

    def test_reader_write_denied_without_checkpoint_mutation(self):
        reader = self.connect("reader")
        before = self.success(reader, "boot", task_id="DENIED-NET")
        denied = self.call(
            reader,
            "checkpoint",
            task_id="DENIED-NET",
            summary="無變更",
            next_step="核對結果",
            status="open",
            expected_revision=0,
            idempotency_key="denied-net-1",
        )
        self.assertTrue(denied.get("error") or denied["result"].get("isError"))
        after = self.success(reader, "boot", task_id="DENIED-NET")
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        self.assertEqual(after["revision"], before["revision"])

    def test_request_id_response_bound_and_raw_secret_diagnostic(self):
        writer = self.connect("writer")
        response = self.client.post(
            self.url,
            headers=writer,
            json={
                "jsonrpc": "2.0",
                "id": "字" * 1000,
                "method": "tools/list",
            },
        )
        self.assertIsNone(response.json()["id"])
        self.assertLess(len(response.content), 512)
        secret = "fixture-password-value"  # noqa: S105 -- synthetic rejected fixture
        denied = self.call(
            writer,
            "ingest_text",
            path="secret.md",
            text=f'# 測試\npassword: "{secret}"',
            expected_sha256="",
            idempotency_key="secret-net-1",
        )
        text = denied["result"]["content"][0]["text"]
        self.assertTrue(denied["result"]["isError"])
        self.assertIn("line 2", text)
        self.assertNotIn(secret, text)

    def test_registry_revocation_downgrade_and_malformed_fail_closed(self):
        reader = self.connect("reader")
        original = self.registry.read_text()
        try:
            records = json.loads(original)
            records.pop("reader")
            self.registry.write_text(json.dumps(records))
            self.assertEqual(
                self.client.post(
                    self.url,
                    headers=reader,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                    },
                ).status_code,
                401,
            )
            self.registry.write_text("{")
            self.assertEqual(
                self.client.post(
                    self.url,
                    headers=reader,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                    },
                ).status_code,
                503,
            )
            self.registry.write_text(original)
            writer = self.connect("writer")
            records = json.loads(original)
            records["writer"]["role"] = "reader"
            self.registry.write_text(json.dumps(records))
            self.assertEqual(
                self.client.post(
                    self.url,
                    headers=writer,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                    },
                ).status_code,
                404,
            )
        finally:
            self.registry.write_text(original)
