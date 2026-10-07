from __future__ import annotations

import math
from typing import Any

import httpx


class HttpEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 8.0,
        dim: int = 1024,
        connect_timeout_s: float = 2.0,
        embed_path: str = "/embed",
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.dim = dim
        self.connect_timeout_s = connect_timeout_s
        self.embed_path = f"/{embed_path.lstrip('/')}"

    def clone_with_timeout(self, timeout_s: float) -> HttpEmbedder:
        return HttpEmbedder(
            base_url=self.base_url,
            timeout_s=timeout_s,
            dim=self.dim,
            connect_timeout_s=min(self.connect_timeout_s, timeout_s),
            embed_path=self.embed_path,
        )

    def embed(self, text: str) -> list[float]:
        embeddings = self.embed_batch([text])
        if not embeddings:
            raise RuntimeError("http embedder returned no embeddings")
        return embeddings[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        timeout = httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s)
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            response = client.post(self.embed_path, json={"texts": texts})
            response.raise_for_status()
            vectors = self._parse_embed_response(response.json())
            if len(vectors) != len(texts):
                raise ValueError("http embedding payload has mismatched vector count")
            return vectors

    def _parse_embed_response(self, payload: dict[str, Any]) -> list[list[float]]:
        if not isinstance(payload, dict):
            raise ValueError("http embedding payload must be an object")
        dimension = payload.get("dimension")
        if not isinstance(dimension, int):
            raise ValueError("http embedding payload missing dimension")
        if dimension != self.dim:
            raise ValueError(f"expected dimension {self.dim}, got {dimension}")

        dense_vecs = payload.get("dense_vecs")
        if not isinstance(dense_vecs, list):
            raise ValueError("http embedding payload missing dense_vecs")

        normalized: list[list[float]] = []
        for item in dense_vecs:
            if not isinstance(item, list):
                raise ValueError("http embedding payload contains invalid vector")
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in item):
                raise ValueError("http embedding payload contains non-numeric value")
            vector = [float(value) for value in item]
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("http embedding payload contains non-finite value")
            if len(vector) != self.dim:
                raise ValueError(f"expected vector length {self.dim}, got {len(vector)}")
            normalized.append(vector)
        return normalized
