"""Real synthetic SQLite checks for namespace migration and bounded retrieval."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from budget import BudgetTooSmall, compact_bytes
from migration import V2_APPLICATION_ID
from store import REMOTE_SCHEMA, SCHEMA, Store


class BatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mh31-batch-store-"))
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.db_path = self.root / "synthetic.sqlite3"
        self.store = Store(self.db_path, self.sources)

    def upload(self, path="spec.md", body="# 匯出\n匯出時限 30 秒。", key="upload", **kwargs):
        return self.store.ingest_text(path, body, "", key, **kwargs)

    def test_namespace_keys_context_binding_and_generation(self):
        first = self.upload(namespace="project:a", actor="claude")
        second = self.upload(namespace="project:b", actor="codex")
        self.assertNotEqual(first["source_id"], second["source_id"])
        a = self.store.boot("SAME", namespace="project:a", actor="claude")["context_id"]
        b = self.store.boot("SAME", namespace="project:b", actor="codex")["context_id"]
        hit_a = self.store.search(a, "匯出", namespace="project:a", actor="claude")["results"][0]
        hit_b = self.store.search(b, "匯出", namespace="project:b", actor="codex")["results"][0]
        self.assertNotEqual(hit_a["section_id"], hit_b["section_id"])
        self.assertEqual(self.store.context_namespace(a, actor="claude"), "project:a")
        for actor, namespace in (("codex", "project:a"), ("claude", "project:b")):
            with self.assertRaises(ValueError):
                self.store.search(a, "匯出", actor=actor, namespace=namespace)
        with self.assertRaises(ValueError):
            self.store.get(a, hit_b["section_id"], actor="claude", namespace="project:a")
        self.store.ingest_text(
            "spec.md",
            "# 匯出\n更新時限 20 秒。",
            first["sha256"],
            "change",
            namespace="project:a",
            actor="claude",
        )
        self.assertEqual(
            self.store.search(a, "匯出", namespace="project:a", actor="claude")["status"],
            "context_stale",
        )
        self.assertEqual(
            self.store.search(b, "匯出", namespace="project:b", actor="codex")["status"], "ok"
        )
        for namespace in ("project:a", "project:b"):
            receipt = self.store.checkpoint(
                "SAME",
                "完成測試",
                "檢查結果",
                "open",
                0,
                "same-key",
                namespace=namespace,
                actor="claude",
            )
            self.assertEqual(receipt["revision"], 1)

    def test_event_dates_provenance_and_explicit_proposals(self):
        self.upload("start.md", key="start", event_date="2026-11-01", freshness="stale")
        self.upload(
            "end.md", key="end", event_date="2026-11-09", summary="人工摘要", summary_kind="manual"
        )
        self.upload("unknown.md", key="unknown")
        self.upload(
            "proposal.md",
            key="proposal",
            record_type="decision",
            lifecycle="proposed",
            event_date="2026-11-09",
        )
        self.upload(
            "superseded.md",
            key="superseded",
            record_type="decision",
            lifecycle="superseded",
            event_date="2026-11-01",
        )
        self.upload(
            "revoked.md",
            key="revoked",
            record_type="decision",
            lifecycle="revoked",
            event_date="2026-11-09",
        )
        self.upload("past.md", key="past", event_date="2026-10-31")
        self.upload("future.md", key="future", event_date="2026-11-10")
        ctx = self.store.boot("DATES")["context_id"]
        hits = self.store.search(
            ctx, "匯出", since="2026-11-01", until="2026-11-09", detail="full"
        )["results"]
        self.assertEqual({r["path"] for r in hits}, {"start.md", "end.md"})
        self.assertEqual(next(r for r in hits if r["path"] == "start.md")["freshness"], "stale")
        end = next(r for r in hits if r["path"] == "end.md")
        self.assertEqual(end["summary_kind"], "manual")
        self.assertEqual(end["summary_sha256"], hashlib.sha256("人工摘要".encode()).hexdigest())
        ctx = self.store.boot("DATES")["context_id"]
        digest = self.store.digest(ctx, "2026-11-01", "2026-11-09")
        self.assertEqual(
            {r["path"] for r in digest["items"]},
            {"start.md", "end.md", "proposal.md", "superseded.md", "revoked.md"},
        )
        self.assertEqual(
            {r["lifecycle"] for r in digest["items"]},
            {"active", "proposed", "superseded", "revoked"},
        )
        proposed = self.store.search(ctx, "匯出", status="proposed")["results"]
        self.assertEqual([r["path"] for r in proposed], ["proposal.md"])
        with self.assertRaises(ValueError):
            self.store.search(ctx, "匯出", since="2026-02-30")
        with self.assertRaises(ValueError):
            self.store.digest(ctx, "2026-11-09", "2026-11-01")

    def test_raw_safety_before_summary_and_persistence(self):
        with patch("store.extractive_summary") as summary:
            with self.assertRaises(ValueError):
                self.upload(body='"password": "fixture-pass"')
            summary.assert_not_called()
        for kwargs in (
            {"summary": '"password": "fixture-pass"', "summary_kind": "manual"},
            {"summary_model": '"password": "fixture-pass"'},
        ):
            with self.assertRaises(ValueError):
                self.upload(**kwargs)
        for summary, next_step in (
            ('"password": "fixture-pass"', "下一步"),
            ("進度", '"password": "fixture-pass"'),
        ):
            with self.assertRaises(ValueError):
                self.store.checkpoint("SAFE", summary, next_step, "open", 0, "unsafe")
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pm_sources").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM pm_checkpoints").fetchone()[0], 0)

    def test_original_bytes_replay_and_concurrent_source_cas(self):
        body = "\ufeff# 匯出\r\n保留原始文字。\r\n"
        first = self.upload(body=body, actor="claude")
        self.assertEqual(first["sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.store.ingest_text(
            "spec.md", "# 新版\n新版文字。", first["sha256"], "next", actor="codex"
        )
        self.assertEqual(self.upload(body=body, actor="claude"), first)
        with self.assertRaises(ValueError):
            self.upload(body="不同文字", actor="claude")
        with sqlite3.connect(self.db_path) as db:
            stored = db.execute(
                "SELECT content FROM pm_source_texts WHERE sha256=?", (first["sha256"],)
            ).fetchone()[0]
            self.assertEqual(stored, body.encode())

        def writer(actor):
            try:
                return self.upload("race.md", key="race", actor=actor)["status"]
            except ValueError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(
                list(pool.map(writer, ("claude", "codex"))), ["indexed", "conflict"]
            )

    def test_same_sha_metadata_cas_and_legacy_noop(self):
        body = "# 匯出\n匯出規格維持不變。"
        first = self.upload(body=body, event_date="2026-11-01")
        before = self.store.source_status("spec.md")
        self.assertEqual(before["metadata_revision"], 1)
        context = self.store.boot("META")["context_id"]

        def writer(pair):
            actor, event_date = pair
            try:
                return self.store.ingest_text(
                    "spec.md",
                    body,
                    first["sha256"],
                    "same-content",
                    actor=actor,
                    event_date=event_date,
                    expected_metadata_revision=1,
                )["status"]
            except ValueError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(
                list(pool.map(writer, (("claude", "2026-11-02"), ("codex", "2026-11-03")))),
                ["indexed", "conflict"],
            )
        self.assertEqual(self.store.source_status("spec.md")["metadata_revision"], 2)
        self.assertEqual(self.store.search(context, "匯出")["status"], "context_stale")
        noop = self.store.ingest_text("spec.md", body, first["sha256"], "legacy-noop")
        self.assertFalse(noop["metadata_changed"])
        self.assertEqual(noop["metadata_revision"], 2)
        ctx = self.store.boot("META")["context_id"]
        current = self.store.search(ctx, "匯出")["results"][0]
        self.assertIn(current["event_date"], ("2026-11-02", "2026-11-03"))
        with self.assertRaises(ValueError):
            self.store.ingest_text(
                "spec.md", body, first["sha256"], "missing-cas", event_date="2026-11-04"
            )

    @staticmethod
    def model_result(summary="模型摘要"):
        return {
            "summary": summary,
            "summary_kind": "model",
            "summary_model": "fixture-model",
            "prompt_sha256": hashlib.sha256(b"fixture prompt").hexdigest(),
        }

    def test_model_raw_request_receipt_replays_when_changed_output_or_offline(self):
        summarizer = Mock()
        summarizer.summarize.return_value = self.model_result()
        args = ("model.md", "# 匯出\n原始匯出規格。", "", "model-request")
        first = self.store.model_ingest_text(*args, summarizer=summarizer, actor="claude")
        summarizer.summarize.return_value = self.model_result("不同的模型輸出")
        self.assertEqual(
            self.store.model_ingest_text(*args, summarizer=summarizer, actor="claude"), first
        )
        summarizer.summarize.assert_called_once()
        newer = self.store.ingest_text(
            "model.md", "# 匯出\n新版規格。", first["sha256"], "newer", actor="codex"
        )
        summarizer.summarize.side_effect = ValueError("synthetic backend offline")
        self.assertEqual(
            self.store.model_ingest_text(*args, summarizer=summarizer, actor="claude"), first
        )
        self.assertEqual(
            self.store.model_ingest_text(*args, summarizer=None, actor="claude"), first
        )
        self.assertEqual(self.store.source_status("model.md")["sha256"], newer["sha256"])
        with self.assertRaises(ValueError):
            self.store.model_ingest_text(
                "model.md",
                "# 匯出\n另一次變更。",
                newer["sha256"],
                "offline-new",
                summarizer=summarizer,
                actor="claude",
            )
        self.assertEqual(self.store.source_status("model.md")["sha256"], newer["sha256"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM pm_write_receipts WHERE actor='claude'"
                ).fetchone()[0],
                1,
            )

    def test_model_raw_changes_and_cross_mode_key_reuse_conflict_before_model(self):
        summarizer = Mock()
        summarizer.summarize.return_value = self.model_result()
        args = ("model.md", "# 匯出\n原始規格。", "", "one-key")
        self.store.model_ingest_text(*args, summarizer=summarizer)
        with self.assertRaises(ValueError):
            self.store.model_ingest_text(*args, summarizer=summarizer, event_date="2026-11-09")
        for kwargs in ({}, {"summary": "人工摘要", "summary_kind": "manual"}):
            with self.assertRaises(ValueError):
                self.store.ingest_text(*args, **kwargs)
        self.upload("plain.md", key="plain-key")
        with self.assertRaises(ValueError):
            self.store.model_ingest_text(
                "plain.md", "# 匯出\n匯出時限 30 秒。", "", "plain-key", summarizer=summarizer
            )
        summarizer.summarize.assert_called_once()

    def test_supplied_source_summary_is_manual_and_new_receipts_identify_actor(self):
        (self.sources / "source.md").write_text("# 匯出\n原始內容。", encoding="utf-8")
        self.store.ingest_source("source.md", summary="人工編寫摘要")
        ctx = self.store.boot("SOURCE")["context_id"]
        source = self.store.search(ctx, "匯出", detail="full")["results"][0]
        self.assertEqual(source["summary_kind"], "manual")
        self.assertEqual(source["summary"], "人工編寫摘要")
        ordinary = self.upload("actor.md", key="actor-write", actor="codex")
        checkpoint = self.store.checkpoint(
            "ACTOR", "完成", "檢查", "open", 0, "actor-checkpoint", actor="codex"
        )
        summarizer = Mock()
        summarizer.summarize.return_value = self.model_result()
        model = self.store.model_ingest_text(
            "actor-model.md",
            "# 匯出\n模型內容。",
            "",
            "actor-model",
            actor="codex",
            summarizer=summarizer,
        )
        self.assertEqual([r["actor"] for r in (ordinary, checkpoint, model)], ["codex"] * 3)

    def test_full_search_small_budget_returns_anchored_pointer(self):
        body = "# 匯出\n" + "大型段落與來源定位。" * 100
        self.upload(body=body)
        ctx = self.store.boot("POINTER")["context_id"]
        response = self.store.search(ctx, "匯出", detail="full", limit=1, budget_bytes=1024)
        self.assertLessEqual(response["used_bytes"], 1024)
        self.assertEqual(response["used_bytes"], len(compact_bytes(response)))
        self.assertEqual(len(response["results"]), 1)
        pointer = response["results"][0]
        self.assertTrue(pointer["expand"])
        self.assertEqual(pointer["freshness"], "unverified")
        self.assertEqual(pointer["lifecycle"], "active")
        self.assertFalse(pointer["historical"])
        self.assertGreater(pointer["omitted_bytes"], 0)
        self.assertGreater(response["truncated_count"], 0)
        self.assertEqual(pointer["sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(
            self.store.get(ctx, pointer["section_id"])["section"]["line_start"],
            pointer["line_start"],
        )

    def test_concurrent_equal_model_retries_invoke_model_once(self):
        summarizer = Mock()
        summarizer.summarize.return_value = self.model_result()

        def invoke(_):
            return self.store.model_ingest_text(
                "model.md", "# 匯出\n原始規格。", "", "race-model", summarizer=summarizer
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(invoke, (1, 2)))
        self.assertEqual(first, second)
        summarizer.summarize.assert_called_once()

    def test_active_decision_capability_cannot_be_bypassed_by_relabeling(self):
        summarizer = Mock()
        summarizer.summarize.return_value = self.model_result()
        for protected_type in ("decision", "requirement"):
            path = f"{protected_type}.md"
            for model in (False, True):
                with self.assertRaises(ValueError):
                    if model:
                        self.store.model_ingest_text(
                            path,
                            "# 核准\n原始定案。",
                            "",
                            "deny-new-model",
                            summarizer=summarizer,
                            record_type=protected_type,
                            allow_active_decisions=False,
                        )
                    else:
                        self.upload(path, record_type=protected_type, allow_active_decisions=False)
            first = self.upload(path, record_type=protected_type, key=protected_type, actor="pm")
            original = self.store.source_status(path)
            for new_type, lifecycle in (("document", "active"), (protected_type, "proposed")):
                with self.assertRaises(ValueError):
                    self.store.ingest_text(
                        path,
                        "# 變更\n改寫定案。",
                        first["sha256"],
                        "relabel",
                        actor="agent",
                        record_type=new_type,
                        lifecycle=lifecycle,
                        allow_active_decisions=False,
                    )
                with self.assertRaises(ValueError):
                    self.store.model_ingest_text(
                        path,
                        "# 變更\n改寫定案。",
                        first["sha256"],
                        "model-relabel",
                        summarizer=summarizer,
                        actor="agent",
                        record_type=new_type,
                        lifecycle=lifecycle,
                        allow_active_decisions=False,
                    )
            self.assertEqual(self.store.source_status(path), original)
        summarizer.summarize.assert_not_called()
        allowed = self.store.ingest_text(
            "decision.md",
            "# 變更\nPM調整內容。",
            self.store.source_status("decision.md")["sha256"],
            "pm-edit",
            actor="pm",
            record_type="document",
            allow_active_decisions=True,
        )
        self.assertEqual(allowed["status"], "indexed")
        proposed = self.upload(
            "proposed.md",
            record_type="decision",
            lifecycle="proposed",
            allow_active_decisions=False,
            key="proposal",
        )
        self.assertEqual(proposed["status"], "indexed")

    def test_per_call_and_cumulative_bytes(self):
        self.upload(body="# 匯出\n" + "匯出驗證與規格。" * 200)
        boot = self.store.boot("BUDGET", budget_bytes=800)
        self.assertEqual(boot["used_bytes"], len(compact_bytes(boot)))
        self.assertLessEqual(boot["used_bytes"], 800)
        ctx = boot["context_id"]
        search = self.store.search(ctx, "匯出", budget_bytes=2200, limit=1)
        section_id = search["results"][0]["section_id"]
        consumed = len(compact_bytes(boot)) + len(compact_bytes(search))
        pointer = self.store.get(ctx, section_id, budget_bytes=700)
        self.assertTrue(pointer["section"]["expand"])
        self.assertEqual(pointer["used_bytes"], len(compact_bytes(pointer)))
        consumed += len(compact_bytes(pointer))
        remaining = pointer["remaining_context_bytes"]
        with self.assertRaises(BudgetTooSmall):
            self.store.get(ctx, section_id, budget_bytes=1)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT remaining_bytes FROM pm_contexts WHERE context_id=?", (ctx,)
                ).fetchone()[0],
                remaining,
            )
        for _ in range(30):
            response = self.store.get(ctx, section_id)
            if response["status"] == "budget_exhausted":
                break
            self.assertEqual(response["used_bytes"], len(compact_bytes(response)))
            consumed += len(compact_bytes(response))
        else:
            self.fail("context must eventually exhaust")
        self.assertLessEqual(consumed, 8000)

    def test_twenty_paired_summary_queries_keep_ids_and_report_sizes(self):
        bodies = {f"Query{i}": f"# Query{i}\n" + "資料規格與驗證流程。" * 50 for i in range(20)}
        for query, body in bodies.items():
            self.upload(f"{query}.md", body, key=query)
        totals = {
            "summary_payload": 0,
            "full_payload": 0,
            "summary_content": 0,
            "source_content": 0,
        }
        for query in bodies:
            pair = {}
            for detail in ("summary", "full"):
                ctx = self.store.boot(query)["context_id"]
                result = self.store.search(ctx, query, detail=detail, limit=1)
                self.assertEqual(result["truncated_count"], 0)
                self.assertFalse(result["has_more"])
                pair[detail] = result
                totals[detail + "_payload"] += len(compact_bytes(result))
            self.assertEqual(
                [r["section_id"] for r in pair["summary"]["results"]],
                [r["section_id"] for r in pair["full"]["results"]],
            )
            totals["summary_content"] += sum(
                len((r["summary"] + r["preview"]).encode()) for r in pair["summary"]["results"]
            )
            totals["source_content"] += sum(
                len(r["text"].encode()) for r in pair["full"]["results"]
            )
        totals["payload_reduction_percent"] = round(
            100 * (1 - totals["summary_payload"] / totals["full_payload"]), 2
        )
        totals["content_reduction_percent"] = round(
            100 * (1 - totals["summary_content"] / totals["source_content"]), 2
        )
        self.assertLess(totals["summary_payload"], totals["full_payload"])
        print("synthetic_paired_summary_20=" + json.dumps(totals, sort_keys=True))


class MigrationTests(unittest.TestCase):
    def test_v1_content_ids_revisions_receipts_and_unknown_dates_are_preserved(self):
        root = Path(tempfile.mkdtemp(prefix="mh31-migrate-")).resolve()
        sources = root / "sources"
        sources.mkdir()
        path = root / "v1.sqlite3"
        data = "# 匯出\n核准版本。\n".encode()
        sha = hashlib.sha256(data).hexdigest()
        section = "a" * 64
        request_hash = hashlib.sha256(
            json.dumps(["old.md", sha, ""], separators=(",", ":")).encode()
        ).hexdigest()
        receipt = '{"status":"indexed","actor":"claude","sha256":"' + sha + '"}'
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA)
            for statement in REMOTE_SCHEMA:
                db.execute(statement)
            db.execute("PRAGMA application_id=1296577329")
            db.executemany(
                "INSERT INTO store_config(key,value) VALUES (?,?)",
                (("sources_root", str(sources)), ("source_generation", "7")),
            )
            db.execute(
                "INSERT INTO sources(path,source_id,current_sha) VALUES ('old.md','original-id',?)",
                (sha,),
            )
            db.execute(
                "INSERT INTO revisions(path,sha256,section_count) VALUES ('old.md',?,1)", (sha,)
            )
            db.execute(
                "INSERT INTO sections(section_id,path,sha256,heading,line_start,line_end,text) "
                "VALUES (?,'old.md',?,'匯出',1,2,?)",
                (section, sha, data.decode()),
            )
            db.execute("INSERT INTO section_fts(section_id,terms) VALUES (?,'匯出')", (section,))
            db.execute(
                "INSERT INTO contexts(context_id,source_generation,remaining_bytes) "
                "VALUES ('old-context',7,6000)"
            )
            db.execute(
                "INSERT INTO checkpoints"
                "(task_id,revision,summary,next_step,status,idempotency_key,request_hash) "
                "VALUES ('K234',3,'原摘要','原下一步','open','legacy','old-hash')"
            )
            db.execute("INSERT INTO task_heads(task_id,revision) VALUES ('K234',3)")
            db.execute(
                "INSERT INTO source_texts(path,sha256,content) VALUES ('old.md',?,?)", (sha, data)
            )
            db.execute(
                "INSERT INTO remote_write_receipts"
                "(actor,operation,idempotency_key,request_hash,receipt) "
                "VALUES ('claude','ingest_text','old-upload',?,?)",
                (request_hash, receipt),
            )
        store = Store(path, sources, default_namespace="project:popdaily")
        self.assertEqual(store.boot("K234")["revision"], 3)
        replay = store.ingest_text("old.md", data.decode(), "", "old-upload", actor="claude")
        self.assertEqual(replay, json.loads(receipt))
        with self.assertRaises(ValueError):
            store.context_namespace("old-context", actor="claude")
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], V2_APPLICATION_ID)
            self.assertEqual(
                db.execute("SELECT section_id FROM pm_sections").fetchone()[0], section
            )
            self.assertEqual(
                db.execute("SELECT source_id FROM pm_sources").fetchone()[0], "original-id"
            )
            self.assertEqual(db.execute("SELECT content FROM pm_source_texts").fetchone()[0], data)
            self.assertEqual(
                db.execute("SELECT receipt FROM pm_write_receipts").fetchone()[0], receipt
            )
            self.assertEqual(
                db.execute(
                    "SELECT event_date,imported_at,record_type,freshness FROM pm_revisions"
                ).fetchone(),
                (None, None, "unknown", "unverified"),
            )
            self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            Store(path, sources, default_namespace="another")
