"""Synthetic integration checks for the local Memory Hall PM adapter."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from store import Store

HERE = Path(__file__).resolve().parent


def encoded_size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mh31-test-"))
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.store = Store(self.root / "pilot.sqlite3", self.sources)

    def test_current_and_historical_sections(self):
        source = self.sources / "export.md"
        source.write_text("# 報表規格\n\n匯出服務 timeout 為 90 秒。\n", encoding="utf-8")
        old = self.store.ingest_source("export.md")
        self.assertTrue(self.store.ingest_source("export.md")["unchanged"])
        context = self.store.boot("REPORT-1")["context_id"]
        old_hit = self.store.search(context, "匯出")["results"][0]
        old_id = old_hit["section_id"]

        source.write_text("# 報表規格\n\n匯出服務 timeout 為 30 秒。\n", encoding="utf-8")
        current = self.store.ingest_source("export.md")
        self.assertNotEqual(current["sha256"], old["sha256"])
        context = self.store.boot("REPORT-1")["context_id"]
        hits = self.store.search(context, "匯出")["results"]
        self.assertTrue(hits)
        self.assertTrue(all(hit["sha256"] == current["sha256"] for hit in hits))
        historical = self.store.get(context, old_id)["section"]
        self.assertTrue(historical["historical"])
        self.assertIn("90", historical["text"])

        source.write_text("# 報表規格\n\n匯出服務 timeout 為 90 秒。\n", encoding="utf-8")
        restored = self.store.ingest_source("export.md")
        self.assertEqual(restored["sha256"], old["sha256"])
        context = self.store.boot("REPORT-1")["context_id"]
        self.assertEqual(self.store.search(context, "匯出")["results"][0]["section_id"], old_id)

    def test_receipts_and_revision_survive_a_new_store(self):
        first = self.store.checkpoint("TASK-1", "測試通過", "審查 diff", "open", 0, "request-1")
        another = Store(self.root / "pilot.sqlite3", self.sources)
        self.assertEqual(
            another.checkpoint("TASK-1", "測試通過", "審查 diff", "open", 0, "request-1"),
            first,
        )
        with self.assertRaises(ValueError):
            another.checkpoint("TASK-1", "另一個結果", "審查 diff", "open", 0, "request-1")
        with self.assertRaises(ValueError):
            another.checkpoint("TASK-1", "結案", "交付", "done", 0, "request-2")
        boot = another.boot("TASK-1")
        self.assertEqual(boot["revision"], 1)
        self.assertIn("審查 diff", json.dumps(boot["checkpoint"], ensure_ascii=False))

    def test_budget_across_search_and_get(self):
        source = self.sources / "export.txt"
        source.write_text(("匯出格式與驗證要求 " * 100 + "\n\n") * 20, encoding="utf-8")
        self.store.ingest_source("export.txt")
        boot = self.store.boot("TASK-1")
        context = boot["context_id"]
        ceiling = encoded_size(boot) + boot["remaining_bytes"]
        consumed = encoded_size(boot)
        first = self.store.search(context, "匯出")
        consumed += encoded_size(first)
        self.assertTrue(first["results"])
        section_id = first["results"][0]["section_id"]
        exhausted = False
        for _ in range(40):
            result = self.store.get(context, section_id)
            if result["status"] == "budget_exhausted":
                exhausted = True
                break
            consumed += encoded_size(result)
            self.assertLessEqual(consumed, ceiling)
        self.assertTrue(exhausted)
        fresh = self.store.boot("TASK-1")
        self.assertGreater(fresh["remaining_bytes"], result["remaining_bytes"])

    def test_source_boundary(self):
        outside = self.root / "outside.md"
        outside.write_text("private fixture", encoding="utf-8")
        (self.sources / "link.md").symlink_to(outside)
        (self.sources / ".hidden.md").write_text("private fixture", encoding="utf-8")
        for path in ("../outside.md", str(outside), "link.md", ".hidden.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.ingest_source(path)

    def test_pilot_database_is_bound_to_its_source_root(self):
        foreign = self.root / "foreign.sqlite3"
        with sqlite3.connect(foreign) as connection:
            connection.execute("CREATE TABLE existing (value TEXT)")
        before = foreign.read_bytes()
        with self.assertRaises(ValueError):
            Store(foreign, self.sources)
        self.assertEqual(foreign.read_bytes(), before)

        other_sources = self.root / "other_sources"
        other_sources.mkdir()
        with self.assertRaises(ValueError):
            Store(self.root / "pilot.sqlite3", other_sources)

    def test_project_boot_prioritizes_blocked_work(self):
        for number in range(6):
            task_id = f"TASK-{number}"
            status = "blocked" if number == 5 else "open"
            self.store.checkpoint(task_id, "short state", "next action", status, 0, task_id)
        self.store.checkpoint("DONE-1", "complete", "archive", "done", 0, "done-1")
        project = self.store.boot("project")
        self.assertTrue(project["has_more"])
        self.assertEqual(len(project["tasks"]), 5)
        self.assertEqual(project["tasks"][0]["task_id"], "TASK-5")
        self.assertNotIn("DONE-1", [task["task_id"] for task in project["tasks"]])

    def test_archived_work_does_not_expand_the_boot_payload(self):
        self.store.checkpoint("ACTIVE", "working", "verify result", "open", 0, "active-1")
        before = self.store.boot("project")
        for number in range(20):
            name = f"old-{number}"
            (self.sources / f"{name}.txt").write_text("歷史工作 " * 100, encoding="utf-8")
            self.store.ingest_source(f"{name}.txt")
            self.store.checkpoint(name, "complete", "archive", "done", 0, name)
        after = self.store.boot("project")
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        self.assertLessEqual(encoded_size(after), encoded_size(before) + 10)

    def test_source_update_stales_previously_assembled_context(self):
        source = self.sources / "export.md"
        source.write_text("# 匯出\n\n規格上限為 90 秒。\n", encoding="utf-8")
        self.store.ingest_source("export.md")
        context = self.store.boot("TASK-1")["context_id"]
        section = self.store.search(context, "匯出")["results"][0]
        self.store.ingest_source("export.md")
        self.assertEqual(self.store.get(context, section["section_id"])["status"], "ok")
        source.write_text("# 匯出\n\n規格上限為 30 秒。\n", encoding="utf-8")
        self.store.ingest_source("export.md")
        self.assertEqual(self.store.search(context, "匯出")["status"], "context_stale")
        self.assertEqual(self.store.get(context, section["section_id"])["status"], "context_stale")


class StdioTests(unittest.TestCase):
    def test_mcp_discovery_and_boot(self):
        root = Path(tempfile.mkdtemp(prefix="mh31-mcp-test-"))
        sources = root / "sources"
        sources.mkdir()
        requests = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            },
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "boot",
                    "arguments": {"task_id": "TASK-1"},
                },
            },
        ]
        # The interpreter and script are fixed, with a synthetic test database.
        output = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(HERE / "server.py"),
                "--db",
                str(root / "pilot.sqlite3"),
                "--sources",
                str(sources),
            ],
            input="".join(json.dumps({"jsonrpc": "2.0", **request}) + "\n" for request in requests),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
        responses = [json.loads(line) for line in output.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(
            {tool["name"] for tool in responses[1]["result"]["tools"]},
            {"ingest_source", "boot", "search", "get", "checkpoint"},
        )
        result = responses[2]["result"]
        self.assertFalse(result.get("isError", False))
        self.assertIn("context_id", json.loads(result["content"][0]["text"]))
