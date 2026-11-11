"""Deterministic preflight checks for the explicitly supported sensitive-data formats."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right

RULE_VERSION = "safety-3"

# These checks intentionally report locations without reproducing matched values.
_PATTERNS = (
    (
        "api_key",
        re.compile(
            r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|"
            r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|cfast_[A-Za-z0-9]{48})\b"
            r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r"|\bauthorization\s*:\s*bearer\s+[A-Za-z0-9_./+=-]{12,}"
            r"|(?:api[_ -]?key|access[_ -]?token|bearer[_ -]?token|\btoken\b|client[_ -]?secret|"
            r"secret[_ -]?(?:access[_ -]?)?key|密鑰|金鑰)\s*[\"']?\s*[:=：]\s*[\"']?"
            r"(?!<|\$\{|\[|(?:none|null|redacted|example|placeholder)\b)"
            r"[A-Za-z0-9_./+=-]{6,}",
            re.IGNORECASE,
        ),
    ),
    (
        "password",
        re.compile(
            r"(?:(?:password|passwd|pwd)\b|密碼|密码)\s*[\"']?\s*[:=：]\s*[\"']?"
            r"(?!<|\$\{|\[|(?:none|null|redacted|example|placeholder)\b)"
            r"[^\s\"'`<>]{4,}",
            re.IGNORECASE,
        ),
    ),
    (
        "email",
        re.compile(
            r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+",
            re.I,
        ),
    ),
    (
        "phone",
        re.compile(
            r"(?<![A-Z0-9])(?:\+?886[ -]?(?:\(0\)[ -]?)?9\d{2}[ -]?\d{3}[ -]?\d{3}"
            r"|09\d{2}[ -]?\d{3}[ -]?\d{3}"
            r"|(?:\+?886[ -]?|0)[2-8][ -]?\d{3,4}[ -]?\d{4})(?![A-Z0-9])",
            re.I,
        ),
    ),
    ("taiwan_id", re.compile(r"(?<![A-Z0-9])[A-Z][12]\d{8}(?![A-Z0-9])", re.I)),
    (
        "quotation",
        re.compile(
            r"(?:報價|报价|底價|底价|合約金額|合同金额|"
            r"\b(?:quotation|quote|quoted price|unit price)\b)"
            # An amount: a currency marker before digits, digits followed by a currency or
            # magnitude unit, a thousands-separated number, or a bare number of four or more
            # digits that is not part of a date, version or identifier. ASCII-only boundaries
            # so a number written right after a CJK character still counts.
            r"[^\n\r]{0,60}?(?:"
            r"(?:(?<![A-Za-z])(?:NT\$|NTD|NT|TWD|USD|RMB|CNY)"
            r"|新台幣|新臺幣|台幣|臺幣|人民幣|美金|美元|[$￥¥])\s*\d"
            r"|\d[\d,.]*\s*(?:元|圓|萬|万|千|仟|億|亿|塊|"
            r"新台幣|新臺幣|台幣|臺幣|人民幣|美金|美元|(?:NTD|TWD|USD|RMB|k)(?![A-Za-z]))"
            r"|(?<![A-Za-z0-9_.])\d{1,3}(?:,\d{3})+(?![A-Za-z0-9_])"
            r"|(?<![A-Za-z0-9_.,/:-])\d{4,}(?![A-Za-z0-9_.,/:年-])"
            r")",
            re.I,
        ),
    ),
)


def scan(text: str) -> list[dict]:
    """Return supported sensitive-data categories and one-based line numbers."""
    if not isinstance(text, str):
        raise ValueError("Safety scan requires text.")
    normalized = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    newlines = [match.start() for match in re.finditer("\n", normalized)]
    findings = {
        (bisect_right(newlines, match.start()) + 1, category)
        for category, pattern in _PATTERNS
        for match in pattern.finditer(normalized)
    }
    return [{"category": category, "line": line} for line, category in sorted(findings)]


def require_safe(text: str) -> None:
    """Reject supported matches without including their text in the exception."""
    findings = scan(text)
    if findings:
        locations = ", ".join(
            f"{item['category']} at line {item['line']}" for item in findings[:20]
        )
        if len(findings) > 20:
            locations += f", and {len(findings) - 20} more locations"
        raise ValueError(f"Sensitive content rejected ({locations}).")


def precheck(arguments: dict) -> dict:
    """Scan every string argument as a write would, without storing or calling a model."""
    import hashlib

    text = arguments.get("text")
    if not isinstance(text, str):
        raise ValueError("Precheck requires text.")
    findings = []
    for field, value in arguments.items():
        if isinstance(value, str):
            findings.extend({"field": field, **item} for item in scan(value))
    return {
        "status": "ok",
        "rule_version": RULE_VERSION,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "safe": not findings,
        "findings": findings,
    }
