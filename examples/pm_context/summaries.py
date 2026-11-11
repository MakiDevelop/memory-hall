"""Bounded extractive summaries and an explicitly configured Ollama adapter."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from safety import require_safe

SUMMARY_CHARACTERS = 300
MODEL_INPUT_BYTES = 64 * 1024
PROMPT_VERSION = "summary-v1"
PROMPT_PATH = Path(__file__).with_name("prompts") / f"{PROMPT_VERSION}.txt"


def _text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Summary source text is required.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Summary source must be valid UTF-8 text.") from None
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError("Summary source contains unsupported control characters.")
    require_safe(text)
    return text


def extractive_summary(text: str) -> str:
    """Return a visibly extractive prefix of at most 300 Unicode codepoints."""
    source = re.sub(r"\s+", " ", _text(text)).strip()
    prefix = "[extractive] "
    limit = SUMMARY_CHARACTERS - len(prefix)
    excerpt = source if len(source) <= limit else source[: limit - 1].rstrip() + "…"
    return prefix + excerpt


class OllamaSummarizer:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 10,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
            raise ValueError("Summary backend URL is invalid.")
        try:
            target = urlsplit(base_url)
            _ = target.port
        except (TypeError, ValueError):
            raise ValueError("Summary backend URL is invalid.") from None
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
        ):
            raise ValueError("Summary backend requires an HTTP(S) URL without credentials.")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("A configured summary model is required.")
        if any(char.isspace() for char in model):
            raise ValueError("Summary model must be one configured model identifier.")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValueError("Summary timeout must be a finite positive number.")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Summary timeout must be a finite positive number.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.transport = transport

    def summarize(self, text: str) -> dict:
        source = _text(text)
        if len(source.encode("utf-8")) > MODEL_INPUT_BYTES:
            raise ValueError("Summary source exceeds the 65536-byte model input limit.")
        try:
            prompt = PROMPT_PATH.read_bytes()
        except OSError:
            raise ValueError("The versioned summary prompt is unavailable.") from None
        request = {
            "model": self.model,
            "system": prompt.decode("utf-8"),
            "prompt": source,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 256},
        }
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.timeout_s,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(f"{self.base_url}/api/generate", json=request)
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException:
            raise ValueError("Summary model timed out; no model summary was saved.") from None
        except (httpx.HTTPError, ValueError):
            raise ValueError("Summary model request failed; no model summary was saved.") from None
        if not isinstance(payload, dict):
            raise ValueError("Summary model returned an invalid response.")
        summary = payload.get("response")
        if payload.get("done") is not True or payload.get("done_reason") == "length":
            raise ValueError("Summary model did not return a complete summary.")
        if "model" in payload and payload["model"] != self.model:
            raise ValueError("Summary backend returned a different model.")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Summary model returned an empty summary.")
        summary = summary.strip()
        if len(summary) > SUMMARY_CHARACTERS:
            raise ValueError("Summary model output exceeds 300 Unicode characters.")
        _text(summary)
        return {
            "summary": summary,
            "summary_kind": "model",
            "summary_model": self.model,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        }
