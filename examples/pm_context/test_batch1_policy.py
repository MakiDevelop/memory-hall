"""Synthetic policy checks with real source files and a mocked LLM transport."""

from __future__ import annotations

import hashlib
import json
import unittest

import httpx
from handoff_template import (
    FIRST_SECTION,
    FIRST_SECTION_BYTES,
    SECTIONS,
    canonicalize,
    template,
    validate_handoff,
)
from safety import require_safe, scan
from summaries import MODEL_INPUT_BYTES, PROMPT_PATH, OllamaSummarizer, extractive_summary


class SafetyTests(unittest.TestCase):
    def test_supported_formats_report_only_category_and_line(self):
        cases = {
            "api_key": [
                "api_key=" + "synthetic_value_123",
                "sk-" + "a" * 25,
                "ghp_" + "a" * 24,
                "cfast_" + "a" * 48,
                "-----BEGIN PRIVATE KEY-----",
                "Authorization: Bearer " + "a" * 24,
                "AWS_SECRET_ACCESS_KEY=" + "a" * 40,
            ],
            "password": [
                "password=" + "fixture-pass",
                "DB_PASSWORD=" + "fixture-pass",
                "密碼：" + "測試密碼值",
            ],
            "email": ["reader@example.invalid"],
            "phone": ["0912-345-678", "+886 912 345 678", "02-23456789"],
            "taiwan_id": ["A123456789"],
            "quotation": ["本案報價新台幣 12345 元", "Quoted price: USD 999"],
        }
        for category, values in cases.items():
            for value in values:
                with self.subTest(category=category, value_length=len(value)):
                    findings = scan("safe fixture\n" + value)
                    self.assertIn({"category": category, "line": 2}, findings)
                    self.assertTrue(all(set(item) == {"category", "line"} for item in findings))
                    with self.assertRaises(ValueError) as caught:
                        require_safe("safe fixture\n" + value)
                    self.assertNotIn(value, str(caught.exception))

    def test_safe_project_metadata_and_placeholders(self):
        text = (
            "namespace=project:synthetic\nevent_date=2026-11-09\n"
            "revision=123456\nsummary_model=fixture-model\n"
            "api_key=<injected-value>\npassword=REDACTED\n"
            "文件含 300 字。原文 SHA 以 metadata 提供。\n"
            "sha256=" + "a" * 64 + "\n"
            "token_sha256=" + "f" * 64 + "\n"
            "source_sha256=abc0212345678def" + "a" * 48 + "\n"
        )
        self.assertEqual(scan(text), [])
        require_safe(text)

    def test_unicode_and_crlf_keep_safe_one_based_locations(self):
        findings = scan("safe\r\nＰＡＳＳＷＯＲＤ＝ｓｙｎｔｈｅｔｉｃ\r\nA123456789")
        self.assertEqual(
            findings,
            [{"category": "password", "line": 2}, {"category": "taiwan_id", "line": 3}],
        )

    def test_repeated_matches_are_deduplicated_per_line(self):
        self.assertEqual(
            scan("first@example.invalid second@example.invalid\nthird@example.invalid"),
            [{"category": "email", "line": 1}, {"category": "email", "line": 2}],
        )


class HandoffTemplateTests(unittest.TestCase):
    def test_template_has_exact_eight_chapters_and_optional_unverified(self):
        sections = validate_handoff(template())
        self.assertEqual(tuple(sections), SECTIONS)
        self.assertEqual(FIRST_SECTION, "先做的三件事")
        self.assertEqual(sections["哪些是「未驗」的"], "")
        self.assertEqual(len(sections[FIRST_SECTION].splitlines()), 3)

    def test_canonical_bytes_are_idempotent(self):
        original = "\ufeff\r\n# e\u0301  \r\n\r\ntext\t\r\n\r\n"
        body = canonicalize(original)
        self.assertEqual(body, "\n# e\u0301  \n\ntext\t\n")
        self.assertEqual(canonicalize(body), body)
        self.assertNotIn("\r", body)
        with self.assertRaises(ValueError):
            canonicalize("bad\x00fixture")
        with self.assertRaises(ValueError):
            canonicalize("bad\ud800fixture")

    def test_missing_duplicate_reordered_and_unknown_chapters_fail(self):
        original = template()
        cases = [
            original.replace("## 相關檔案指標", "### 相關檔案指標"),
            original + "\n## 相關檔案指標\n重複\n",
            original.replace("## 待決事項／待問", "## 自訂額外章節"),
            original.replace("## 已知坑與教訓", "## 機器與腳本位置"),
        ]
        for body in cases:
            with self.subTest(length=len(body)), self.assertRaises(ValueError):
                validate_handoff(body)

    def test_three_actions_remain_whole_with_a_bounded_utf8_size(self):
        original = template()
        first = validate_handoff(original)[FIRST_SECTION]
        oversized = "1. " + "合" * FIRST_SECTION_BYTES + "\n2. 驗證。\n3. 交接。"
        cases = [
            original.replace(first, "1. 只有一個動作。"),
            original.replace(first, first + "\n4. 第四個動作。"),
            original.replace(first, "1. \n2. 驗證。\n3. 交接。"),
            original.replace(first, oversized),
        ]
        for body in cases:
            with self.subTest(length=len(body)), self.assertRaises(ValueError):
                validate_handoff(body)

    def test_numbered_headings_and_fenced_source_examples(self):
        body = template()
        for index, title in enumerate(SECTIONS, start=1):
            body = body.replace(f"## {title}", f"## {index}. {title}")
        body += "\n```markdown\n## source-heading-only\n```\n"
        sections = validate_handoff(body)
        self.assertIn("## source-heading-only", sections[SECTIONS[-1]])

    def test_unfilled_required_chapter_and_sensitive_body_fail(self):
        body = template()
        sections = validate_handoff(body)
        without_required = body.replace(
            "## 機器與腳本位置\n\n" + sections["機器與腳本位置"], "## 機器與腳本位置"
        )
        with self.assertRaises(ValueError):
            validate_handoff(without_required)
        with self.assertRaises(ValueError):
            validate_handoff(body + "\nreader@example.invalid\n")


class SummaryTests(unittest.TestCase):
    def test_extract_is_bounded_marked_and_unicode_safe(self):
        short = extractive_summary("合成來源\n\n狀態待查，下一步比對來源。")
        self.assertTrue(short.startswith("[extractive] "))
        self.assertIn("狀態待查", short)
        long = extractive_summary("合成來源" * 200)
        self.assertEqual(len(long), 300)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(long.encode().decode(), long)

    def test_scan_runs_on_full_source_before_extraction(self):
        source = "合成來源 " * 200 + " reader@example.invalid"
        with self.assertRaises(ValueError):
            extractive_summary(source)

    def test_model_uses_configured_request_and_versioned_prompt(self):
        captured = []

        def generate(request):
            captured.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://model.example/api/generate")
            return httpx.Response(
                200, json={"done": True, "model": "fixture-model", "response": "待查合成來源。"}
            )

        adapter = OllamaSummarizer(
            "https://model.example", "fixture-model", timeout_s=0.2,
            transport=httpx.MockTransport(generate),
        )
        summary = adapter.summarize("只有合成內容，尚未完成驗證。")
        self.assertEqual(summary["summary"], "待查合成來源。")
        self.assertEqual(summary["summary_kind"], "model")
        self.assertEqual(summary["summary_model"], "fixture-model")
        self.assertEqual(summary["prompt_version"], "summary-v1")
        self.assertEqual(
            summary["prompt_sha256"], hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
        )
        self.assertEqual(captured[0]["model"], "fixture-model")
        self.assertEqual(captured[0]["system"], PROMPT_PATH.read_text())
        self.assertEqual(captured[0]["options"]["num_predict"], 256)
        self.assertFalse(captured[0]["stream"])

    def test_unsafe_or_oversized_sources_do_not_reach_model(self):
        calls = []

        def generate(request):
            calls.append(request)
            return httpx.Response(200, json={"done": True, "response": "合成。"})

        adapter = OllamaSummarizer(
            "https://model.example", "fixture-model", transport=httpx.MockTransport(generate)
        )
        for source in ("reader@example.invalid", "a" * (MODEL_INPUT_BYTES + 1)):
            with self.subTest(size=len(source)), self.assertRaises(ValueError):
                adapter.summarize(source)
        self.assertEqual(calls, [])

    def test_timeout_and_backend_errors_do_not_echo_or_fallback(self):
        def timeout(request):
            raise httpx.ReadTimeout("untrusted backend detail", request=request)

        handlers = [
            timeout,
            lambda _: httpx.Response(503, text="untrusted backend detail"),
            lambda _: httpx.Response(200, text="not-json untrusted backend detail"),
            lambda _: httpx.Response(302, headers={"location": "https://other.example"}),
        ]
        for handler in handlers:
            adapter = OllamaSummarizer(
                "https://model.example", "fixture-model", transport=httpx.MockTransport(handler)
            )
            with self.subTest(handler=handler), self.assertRaises(ValueError) as caught:
                adapter.summarize("合成來源。")
            self.assertNotIn("untrusted backend detail", str(caught.exception))

    def test_invalid_or_unsafe_model_output_is_rejected(self):
        cases = [
            [],
            {"done": False, "response": "合成。"},
            {"done": True, "done_reason": "length", "response": "合成。"},
            {"done": True, "model": "another-model", "response": "合成。"},
            {"done": True, "response": ""},
            {"done": True, "response": "合" * 301},
            {"done": True, "response": "reader@example.invalid"},
            {"done": True, "response": "unsafe\x00fixture"},
        ]
        for payload in cases:
            adapter = OllamaSummarizer(
                "https://model.example", "fixture-model",
                transport=httpx.MockTransport(
                    lambda _, value=payload: httpx.Response(200, json=value)
                ),
            )
            with self.subTest(payload_keys=list(payload)), self.assertRaises(ValueError):
                adapter.summarize("合成來源。")

    def test_backend_configuration_has_explicit_validation(self):
        for url in ("file:///tmp/source", "http://user:password@model.example", "http://model.example?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaSummarizer(url, "fixture-model")
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                OllamaSummarizer("https://model.example", "fixture-model", timeout_s=timeout)


if __name__ == "__main__":
    unittest.main()
