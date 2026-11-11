"""Handoff publication/recovery against real synthetic files and SQLite."""

import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from budget import compact_bytes
from handoff import write_immutable
from handoff_template import FIRST_SECTION, SECTIONS, canonicalize, template, validate_handoff
from store import Store


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mh31-handoff-"))
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.roots = {name: self.root / name for name in ("archive", "pm_hub", "notes")}
        for root in self.roots.values():
            root.mkdir()
        self.db = self.root / "synthetic.sqlite3"
        self.store = Store(self.db, self.sources, handoff_destinations=self.roots)
        self.body = template().replace("待填第一項具體動作。", "核對匯出規格。")

    def write(self, body=None, revision=0, key="handoff", **kwargs):
        return self.store.handoff_write(
            "MAIN", self.body if body is None else body, revision, key, **kwargs
        )

    def read(self):
        return self.store.handoff_read(self.store.boot("MAIN")["context_id"], "MAIN")

    def test_three_destinations_same_canonical_bytes_and_idempotence(self):
        body = "\ufeff" + self.body.replace("\n", "\r\n")
        receipt = self.write(body, actor="claude")
        self.assertEqual(receipt["state"], "published")
        canonical = canonicalize(body).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        self.assertEqual(receipt["database_receipt"]["sha256"], digest)
        for destination in receipt["destinations"]:
            self.assertEqual(destination["actual_sha"], digest)
            self.assertTrue(destination["receipt"]["readback"])
            self.assertEqual(Path(destination["receipt"]["path"]).read_bytes(), canonical)
        self.assertEqual(self.write(body, actor="claude"), receipt)
        self.assertEqual(self.read()["body"], canonical.decode())
        with self.assertRaises(ValueError):
            self.write(self.body.replace("核對匯出規格。", "改另一個動作。"), actor="claude")
        with self.assertRaises(ValueError):
            self.write(key="stale")
        with self.assertRaises(ValueError):
            self.store.handoff_status(receipt["operation_id"], namespace="project:other")

    def test_pending_not_indexed_and_retry_after_restart(self):
        missing = self.root / "offline-notes"
        roots = {**self.roots, "notes": missing}
        self.store = Store(self.db, self.sources, handoff_destinations=roots)
        receipt = self.write()
        self.assertEqual(receipt["state"], "pending")
        self.assertIsNone(receipt["database_receipt"])
        self.assertEqual(self.read()["status"], "not_found")
        ctx = self.store.boot("MAIN")["context_id"]
        self.assertFalse(self.store.search(ctx, "匯出")["results"])
        attempts = {r["destination_id"]: r["attempts"] for r in receipt["destinations"]}
        missing.mkdir()
        resumed = Store(self.db, self.sources, handoff_destinations=roots)
        done = resumed.handoff_retry(receipt["operation_id"], actor="codex")
        self.assertEqual(done["state"], "published")
        counts = {r["destination_id"]: r["attempts"] for r in done["destinations"]}
        self.assertEqual(counts["archive"], attempts["archive"])
        self.assertEqual(counts["pm_hub"], attempts["pm_hub"])
        self.assertEqual(counts["notes"], attempts["notes"] + 1)

    def test_pending_revision_preserves_previous_published_and_old_context(self):
        first = self.write()
        context = self.store.boot("MAIN")["context_id"]
        original = write_immutable

        def unavailable(root, *args):
            if root == self.roots["notes"]:
                raise OSError("synthetic offline")
            return original(root, *args)

        changed = self.body.replace("核對匯出規格。", "核對新版匯出規格。")
        with patch("handoff.write_immutable", side_effect=unavailable):
            pending = self.write(changed, 1, "second")
        self.assertEqual(pending["state"], "pending")
        read = self.store.handoff_read(context, "MAIN")
        self.assertEqual(read["revision"], 1)
        self.assertEqual(read["sha256"], first["sha256"])
        with self.assertRaises(ValueError):
            self.write(changed, 2, "third")
        done = self.store.handoff_retry(pending["operation_id"])
        self.assertEqual(done["state"], "published")
        self.assertEqual(self.store.handoff_read(context, "MAIN")["status"], "context_stale")
        self.assertEqual(self.read()["revision"], 2)

    def test_crash_after_file_before_receipt_recovers_without_replacing(self):
        original = write_immutable
        crashed = False

        def crash(root, *args):
            nonlocal crashed
            receipt = original(root, *args)
            if root == self.roots["notes"] and not crashed:
                crashed = True
                raise OSError("synthetic interrupted receipt")
            return receipt

        with patch("handoff.write_immutable", side_effect=crash):
            first = self.write()
        self.assertEqual(first["state"], "pending")
        artifact = next(self.roots["notes"].glob("*.md"))
        before = artifact.stat().st_ino
        done = self.store.handoff_retry(first["operation_id"])
        self.assertEqual(done["state"], "published")
        self.assertEqual(artifact.stat().st_ino, before)

    def test_changed_destination_is_not_overwritten(self):
        missing = self.root / "initially-offline"
        self.store = Store(
            self.db, self.sources, handoff_destinations={**self.roots, "notes": missing}
        )
        pending = self.write()
        missing.mkdir()
        filename = Path(pending["destinations"][0]["receipt"]["path"]).name
        target = missing / filename
        target.write_text("Another writer's document.")
        done = self.store.handoff_retry(pending["operation_id"])
        self.assertEqual(done["state"], "pending")
        self.assertEqual(target.read_text(), "Another writer's document.")
        notes = next(r for r in done["destinations"] if r["destination_id"] == "notes")
        self.assertEqual(notes["error_code"], "content_conflict")
        self.assertEqual(self.read()["status"], "not_found")

    def test_completed_target_changed_before_publication_is_rejected(self):
        missing = self.root / "offline-final"
        self.store = Store(
            self.db, self.sources, handoff_destinations={**self.roots, "notes": missing}
        )
        pending = self.write()
        archive = next(r for r in pending["destinations"] if r["destination_id"] == "archive")
        target = Path(archive["receipt"]["path"])
        target.write_text("Changed after its original readback.")
        missing.mkdir()
        result = self.store.handoff_retry(pending["operation_id"])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(target.read_text(), "Changed after its original readback.")
        archive = next(r for r in result["destinations"] if r["destination_id"] == "archive")
        self.assertEqual(archive["error_code"], "content_conflict")
        self.assertEqual(self.read()["status"], "not_found")

    def test_large_read_preserves_actions_and_work_pending_previews(self):
        body = self.body.replace(
            "## " + SECTIONS[3] + "\n", "## " + SECTIONS[3] + "\n進行中的工作。" * 1 + "\n"
        )
        body = body.replace(
            "## " + SECTIONS[5] + "\n", "## " + SECTIONS[5] + "\n" + "大量歷史紀錄。" * 1000 + "\n"
        )
        sections = validate_handoff(body)
        self.write(body)
        read = self.read()
        self.assertLessEqual(read["used_bytes"], 4096)
        self.assertEqual(read["used_bytes"], len(compact_bytes(read)))
        self.assertIn(sections[FIRST_SECTION], read["body"])
        self.assertEqual(
            [r["chapter"] for r in read["chapter_previews"]], [SECTIONS[3], SECTIONS[4]]
        )
        self.assertTrue(read["expand"])
        self.assertGreater(read["truncated_count"], 0)

    def test_racing_handoff_writers_and_safety_gate(self):
        def writer(actor):
            try:
                return self.write(actor=actor)["state"]
            except ValueError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(
                list(pool.map(writer, ("claude", "codex"))), ["published", "conflict"]
            )
        with self.assertRaises(ValueError):
            self.write(
                self.body.replace("核對匯出規格。", '"password": "fixture-pass"'), 1, "unsafe"
            )
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pm_handoff_commits").fetchone()[0], 1)
        no_config = Store(self.db, self.sources)
        with self.assertRaises(ValueError):
            no_config.handoff_write("OTHER", self.body, 0, "no-config")
