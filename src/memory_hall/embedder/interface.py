from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    dim: int
    timeout_s: float

    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
