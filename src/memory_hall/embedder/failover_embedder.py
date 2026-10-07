from __future__ import annotations

import json
import logging
import time
from copy import copy
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

import httpx

from memory_hall.embedder.http_embedder import HttpEmbedder

logger = logging.getLogger(__name__)
State = Literal["healthy", "cooling_down", "mismatch"]


@dataclass
class _Backend:
    embedder: HttpEmbedder
    # Unverified backends are eligible immediately, but never reported healthy.
    state: State = "cooling_down"
    retry_at: float = 0.0
    verified: bool = False
    generation: int = 0
    probe_lock: Lock = field(default_factory=Lock)
    error: Exception = field(default_factory=lambda: httpx.ConnectError("backend not checked"))


@dataclass
class _Shared:
    backends: list[_Backend]
    lock: Lock = field(default_factory=Lock)
    last_backend: str | None = None


class FailoverEmbedder:
    """Ordered HTTP failover with lazy recovery and shared state across timeout views."""

    def __init__(
        self,
        backends: list[HttpEmbedder],
        *,
        model: str = "BAAI/bge-m3",
        cooldown_s: float = 60.0,
        mismatch_recheck_s: float = 600.0,
    ) -> None:
        if not backends or len({backend.dim for backend in backends}) != 1:
            raise ValueError("failover requires backends with matching dimensions")
        self.dim = backends[0].dim
        self.model = model
        self.cooldown_s = cooldown_s
        self.mismatch_recheck_s = mismatch_recheck_s
        # Two requests on first use/recovery: health then embed. Include HTTP
        # connect/write/read/pool phases; the runtime retains a hard outer cap.
        self.timeout_s = sum(
            2 * (backend.connect_timeout_s + 3 * backend.timeout_s) for backend in backends
        )
        self._shared = _Shared([_Backend(backend) for backend in backends])

    def clone_with_timeout(self, timeout_s: float) -> FailoverEmbedder:
        clone = copy(self)
        clone.timeout_s = timeout_s
        return clone

    def health_snapshot(self) -> dict:
        with self._shared.lock:
            return {
                "embed_backends": [
                    {"index": index, "state": backend.state}
                    for index, backend in enumerate(self._shared.backends)
                ],
                "last_embed_backend_index": next(
                    (index for index, backend in enumerate(self._shared.backends)
                     if backend.embedder.base_url == self._shared.last_backend),
                    None,
                ),
            }

    def _transition(self, backend: _Backend, state: State) -> None:
        # Caller holds state lock. Do not log payloads, exception text or tokens.
        if backend.state != state:
            logger.info("embed backend=%s state=%s", backend.embedder.base_url, state)
            backend.state = state

    def _client_view(self, backend: _Backend, deadline: float) -> HttpEmbedder:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("embedding failover time budget exhausted")
        return backend.embedder.clone_with_timeout(min(backend.embedder.timeout_s, remaining))

    def _verify(self, backend: _Backend, deadline: float) -> bool:
        embedder = self._client_view(backend, deadline)
        timeout = httpx.Timeout(embedder.timeout_s, connect=embedder.connect_timeout_s)
        with httpx.Client(base_url=embedder.base_url, timeout=timeout) as client:
            response = client.get("/health")
            response.raise_for_status()
            payload = response.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("model"), str)
                or type(payload.get("dimension")) is not int):
            raise ValueError("invalid embedding health payload")
        if payload["model"] != self.model or payload["dimension"] != self.dim:
            with self._shared.lock:
                backend.error = ValueError("embedding backend model/dimension mismatch")
                backend.verified = False
                backend.generation += 1
                backend.retry_at = time.monotonic() + self.mismatch_recheck_s
                self._transition(backend, "mismatch")
            return False
        return True

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        deadline = time.monotonic() + self.timeout_s
        return self._embed_batch(texts, deadline)

    def _failure(self, backend: _Backend, exc: Exception, generation: int) -> Exception:
        # Preserve error classes, without remote payloads or tokens in messages.
        if isinstance(exc, httpx.HTTPStatusError):
            error = httpx.HTTPStatusError(
                f"embedding backend HTTP {exc.response.status_code}",
                request=exc.request, response=exc.response,
            )
        elif isinstance(exc, json.JSONDecodeError):
            error = json.JSONDecodeError("invalid embedding JSON", "", 0)
        else:
            error = type(exc)(f"embedding backend {type(exc).__name__}")
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            raise error from None
        with self._shared.lock:
            if backend.generation == generation:
                backend.error = error
                backend.verified = False
                backend.generation += 1
                backend.retry_at = time.monotonic() + self.cooldown_s
                self._transition(backend, "cooling_down")
        return error

    def _prepare(self, backend: _Backend, deadline: float) -> int | None:
        with self._shared.lock:
            if time.monotonic() < backend.retry_at:
                return None
            if backend.verified:
                return backend.generation
        # Wait briefly for this backend's probe, never for its embed call.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("embedding failover time budget exhausted")
        if not backend.probe_lock.acquire(timeout=min(0.05, remaining)):
            return None
        try:
            with self._shared.lock:
                if time.monotonic() < backend.retry_at:
                    return None
                if backend.verified:
                    return backend.generation
                generation = backend.generation
            try:
                if not self._verify(backend, deadline):
                    return None
            except (httpx.HTTPError, ValueError) as exc:
                raise self._failure(backend, exc, generation) from None
            with self._shared.lock:
                backend.verified = True
                backend.retry_at = 0.0
                self._transition(backend, "healthy")
                return backend.generation
        finally:
            backend.probe_lock.release()

    def _embed_batch(self, texts: list[str], deadline: float) -> list[list[float]]:
        last_error: Exception = httpx.ConnectError("no embedding backend available")
        for backend in self._shared.backends:
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout("embedding failover time budget exhausted")
            try:
                generation = self._prepare(backend, deadline)
            except (httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                last_error = exc
                continue
            if generation is None:
                with self._shared.lock:
                    last_error = backend.error
                continue
            try:
                embedder = self._client_view(backend, deadline)
                vectors = embedder.embed_batch(texts)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = self._failure(backend, exc, generation)
                continue
            with self._shared.lock:
                # A late success must not undo a concurrent failure or mismatch.
                self._shared.last_backend = backend.embedder.base_url
            return vectors
        raise last_error from None
