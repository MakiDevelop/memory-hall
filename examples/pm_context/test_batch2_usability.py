"""Batch 2 usability: schema errors that stop loops, session quotas, precheck, quotation rule."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from http_server import create_app
from safety import RULE_VERSION, precheck, scan
from server import RepeatGuard
from store import Store

PROTOCOL = "2025-11-25"


def _principals(*actors):
    return {
        actor: {
            "token_sha256": hashlib.sha256(f"token-{actor}".encode()).hexdigest(),
            "role": "agent_writer",
        }
        for actor in actors
    }


class Harness:
    def __init__(self, test, actors=("a", "b"), **options):
        root = Path(tempfile.mkdtemp(prefix="mh31-batch2-"))
        (root / "sources").mkdir()
        self.principals = _principals(*actors)
        self.app = create_app(
            Store(root / "db.sqlite3", root / "sources"),
            self.principals,
            extended=True,
            **options,
        )
        self.client = TestClient(self.app)
        test.addCleanup(self.client.close)

    def headers(self, actor, session=None):
        value = {"Authorization": f"Bearer token-{actor}"}
        if session:
            value["Mcp-Session-Id"] = session
        return value

    def initialize(self, actor):
        response = self.client.post(
            "/mcp",
            headers=self.headers(actor),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL},
            },
        )
        if response.status_code != 200:
            return response, None
        session = response.headers["Mcp-Session-Id"]
        self.client.post(
            "/mcp",
            headers=self.headers(actor, session),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return response, session

    def call(self, actor, session, name, arguments):
        response = self.client.post(
            "/mcp",
            headers=self.headers(actor, session),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        return json.loads(response.json()["result"]["content"][0]["text"])


class SchemaErrorTests(unittest.TestCase):
    def test_error_names_missing_unknown_and_example_then_stops(self):
        h = Harness(self)
        _, session = h.initialize("a")
        bad = {"task_id": "t", "summary": "s", "bogus": "x"}
        first = h.call("a", session, "checkpoint", bad)
        self.assertEqual(
            first["missing"], ["expected_revision", "idempotency_key", "next_step", "status"]
        )
        self.assertEqual(first["unknown"], ["bogus"])
        self.assertEqual(first["example"]["expected_revision"], 0)
        self.assertNotIn("stop_retrying", first)
        self.assertNotIn("stop_retrying", h.call("a", session, "checkpoint", bad))
        third = h.call("a", session, "checkpoint", bad)
        self.assertTrue(third["stop_retrying"])
        self.assertEqual(third["attempt"], 3)

    def test_reconnect_does_not_reset_and_other_identity_unaffected(self):
        h = Harness(self)
        bad = {"task_id": "t"}
        _, session = h.initialize("a")
        for _ in range(2):
            h.call("a", session, "checkpoint", bad)
        _, reconnected = h.initialize("a")
        self.assertTrue(h.call("a", reconnected, "checkpoint", bad)["stop_retrying"])
        _, other = h.initialize("b")
        self.assertNotIn("stop_retrying", h.call("b", other, "checkpoint", bad))

    def test_valid_call_still_works_after_stop_and_clears_counter(self):
        h = Harness(self)
        _, session = h.initialize("a")
        for _ in range(3):
            h.call("a", session, "boot", {})
        ok = h.call("a", session, "boot", {"task_id": "t"})
        self.assertEqual(ok["status"], "ok")
        self.assertNotIn("stop_retrying", h.call("a", session, "boot", {}))

    def test_different_errors_count_separately_and_window_expires(self):
        now = [0.0]
        guard = RepeatGuard(threshold=3, window_seconds=10, clock=lambda: now[0])
        h = Harness(self, guard=guard)
        _, session = h.initialize("a")
        h.call("a", session, "boot", {})
        h.call("a", session, "boot", {"task_id": 1})
        self.assertNotIn("stop_retrying", h.call("a", session, "boot", {"task_id": "t", "x": 1}))
        h.call("a", session, "boot", {})
        self.assertTrue(h.call("a", session, "boot", {}).get("stop_retrying"))
        now[0] = 100.0
        self.assertNotIn("stop_retrying", h.call("a", session, "boot", {}))

    def test_wrong_type_reported(self):
        h = Harness(self)
        _, session = h.initialize("a")
        result = h.call("a", session, "boot", {"task_id": 5})
        self.assertEqual(result["wrong_type"], {"task_id": "expected string"})


class RepeatGuardBoundTests(unittest.TestCase):
    def test_memory_is_bounded_and_existing_key_is_not_self_evicted(self):
        guard = RepeatGuard(threshold=3, window_seconds=600, max_keys=4, clock=lambda: 0.0)
        for index in range(4):
            guard.record(("a", None, "boot", str(index)))
        self.assertEqual(guard.record(("a", None, "boot", "0")), 2)
        self.assertEqual(guard.record(("a", None, "boot", "0")), 3)
        for index in range(100):
            guard.record(("a", None, "boot", f"new-{index}"))
        self.assertLessEqual(len(guard._counts), 4)

    def test_expired_keys_are_pruned_on_every_record(self):
        now = [0.0]
        guard = RepeatGuard(threshold=3, window_seconds=10, max_keys=100, clock=lambda: now[0])
        for index in range(50):
            guard.record(("a", None, "boot", str(index)))
        now[0] = 100.0
        guard.record(("a", None, "boot", "fresh"))
        self.assertEqual(len(guard._counts), 1)

    def test_long_names_do_not_grow_keys_or_responses(self):
        guard = RepeatGuard()
        h = Harness(self, guard=guard)
        _, session = h.initialize("a")
        long_name = "x" * 10_000
        result = h.call("a", session, "boot", {"task_id": "t", long_name: "v"})
        self.assertEqual(result["unknown"], ["x" * 64])
        (key,) = guard._counts
        self.assertTrue(all(len(str(part)) <= 64 for part in key))

    def test_unknown_tool_stop_is_a_boolean(self):
        h = Harness(self)
        _, session = h.initialize("a")
        for attempt in range(1, 4):
            response = h.client.post(
                "/mcp",
                headers=h.headers("a", session),
                json={
                    "jsonrpc": "2.0",
                    "id": attempt,
                    "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}},
                },
            ).json()
            self.assertIs(response["error"]["data"]["stop_retrying"], attempt >= 3)


class SessionQuotaTests(unittest.TestCase):
    def test_one_identity_cannot_starve_another(self):
        h = Harness(self, max_sessions_per_principal=2)
        sessions = [h.initialize("a")[1] for _ in range(2)]
        denied, none = h.initialize("a")
        self.assertEqual(denied.status_code, 503)
        self.assertIsNone(none)
        self.assertEqual(h.initialize("b")[0].status_code, 200)
        # DELETE returns the slot.
        h.client.delete("/mcp", headers=h.headers("a", sessions[0]))
        self.assertEqual(h.initialize("a")[0].status_code, 200)

    def test_rotated_token_hash_reclaims_old_sessions(self):
        h = Harness(self, max_sessions_per_principal=1)
        _, old = h.initialize("a")
        h.principals["a"] = {
            "token_sha256": hashlib.sha256(b"rotated-token").hexdigest(),
            "role": "agent_writer",
        }
        response = h.client.post(
            "/mcp",
            headers={"Authorization": "Bearer rotated-token"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(old)

    def test_idle_and_revoked_sessions_free_quota(self):
        import http_server

        h = Harness(self, max_sessions_per_principal=1)
        _, session = h.initialize("a")
        self.assertEqual(h.initialize("a")[0].status_code, 503)
        # Revoke a's credential; b's next request reclaims a's session slot.
        del h.principals["a"]
        h.principals.update(_principals("c"))
        self.assertEqual(h.initialize("b")[0].status_code, 200)
        self.assertEqual(h.initialize("a")[0].status_code, 401)
        self.assertIsNotNone(session)
        self.assertGreater(http_server.MAX_SESSIONS, 1)


class PrecheckTests(unittest.TestCase):
    def test_matches_write_time_scan_and_stores_nothing(self):
        h = Harness(self)
        _, session = h.initialize("a")
        text = "safe\npassword=" + "fixture-pass"
        result = h.call("a", session, "precheck", {"text": text, "summary": "ok", "path": "a.md"})
        self.assertEqual(result["rule_version"], RULE_VERSION)
        self.assertEqual(result["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertFalse(result["safe"])
        self.assertEqual(result["findings"], [{"field": "text", "category": "password", "line": 2}])
        self.assertNotIn("fixture-pass", json.dumps(result))
        write = h.call(
            "a",
            session,
            "ingest_text",
            {"path": "a.md", "text": text, "expected_sha256": "", "idempotency_key": "k1"},
        )
        self.assertIn("password at line 2", write["error"])
        self.assertEqual(h.call("a", session, "source_status", {"path": "a.md"})["sha256"], "")

    def test_metadata_fields_are_scanned(self):
        result = precheck({"text": "fine", "summary": "mail reader@example.invalid"})
        self.assertEqual(result["findings"], [{"field": "summary", "category": "email", "line": 1}])

    def test_clean_text(self):
        self.assertTrue(precheck({"text": "hello"})["safe"])


class QuotationRuleTests(unittest.TestCase):
    def test_innocent_numbers_after_the_keyword_pass(self):
        for text in (
            "報價流程見第 3 點，共 2 步",
            "報價規則 v2 於 11/9 定案",
            "quote the 3 items from sprint 12",
            "底價機制 K552 還沒定",
            "報價流程 2026-11-09 定案",
            "報價規則於 2026年 改版",
            "報價表 v1.2.3 已更新",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_real_quotations_are_still_blocked(self):
        for text in (
            "本案報價新台幣 12345 元",
            "Quoted price: USD 999",
            "報價 5 萬",
            "報價：1,200,000",
            "報價 120000",
            "unit price $50",
            "底價 80000 元",
            "報價12000",
            "報價為5000台幣",
            "報價 500 台幣",
            "報價5千",
            "報價 NTD 300",
            "報價：3億",
        ):
            with self.subTest(text=text):
                self.assertIn("quotation", {item["category"] for item in scan(text)})


class CliEdgeTests(unittest.TestCase):
    def _client(self, env):
        import os
        from unittest import mock

        from memory_hall.cli.pm import Client

        with mock.patch.dict(os.environ, env, clear=False):
            return Client("https://mh-test.example.invalid/mcp", "synthetic-token")

    def test_gzip_response_from_an_edge_is_decoded_once(self):
        import gzip

        import httpx

        def handler(request):
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, content=gzip.compress(body)
            )

        client = self._client({})
        client.http = httpx.Client(transport=httpx.MockTransport(handler))
        response = client.request("ping", {})
        self.assertEqual(response.json()["id"], 1)

    def test_access_headers_and_proxy_are_opt_in(self):
        client = self._client({})
        self.assertNotIn("CF-Access-Client-Id", client.http.headers)
        self.assertFalse(client.http._trust_env)
        client = self._client(
            {
                "MEMORY_HALL_31_CF_ACCESS_CLIENT_ID": "synthetic.access",
                "MEMORY_HALL_31_CF_ACCESS_CLIENT_SECRET": "synthetic-secret",
                "MEMORY_HALL_31_USE_ENV_PROXY": "1",
            }
        )
        self.assertEqual(client.http.headers["CF-Access-Client-Id"], "synthetic.access")
        self.assertTrue(client.http._trust_env)
        with self.assertRaises(ValueError):
            self._client({"MEMORY_HALL_31_CF_ACCESS_CLIENT_ID": "only-id"})

    def test_env_proxy_is_never_used_for_loopback_http(self):
        import os
        from unittest import mock

        from memory_hall.cli.pm import Client

        with mock.patch.dict(os.environ, {"MEMORY_HALL_31_USE_ENV_PROXY": "1"}):
            client = Client("http://127.0.0.1:8766/mcp", "synthetic-token")
        self.assertFalse(client.http._trust_env)


if __name__ == "__main__":
    unittest.main()
