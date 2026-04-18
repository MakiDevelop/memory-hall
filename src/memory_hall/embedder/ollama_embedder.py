from __future__ import annotations

from typing import Any

import httpx


class OllamaEmbedder:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "bge-m3",
        timeout_s: float = 2.0,
        dim: int = 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        embeddings = self.embed_batch([text])
        if not embeddings:
            raise RuntimeError("ollama returned no embeddings")
        return embeddings[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        timeout = httpx.Timeout(self.timeout_s)
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            try:
                response = client.post(
                    "/api/embed",
                    json={"model": self.model, "input": texts if len(texts) > 1 else texts[0]},
                )
                response.raise_for_status()
                vectors = self._parse_embed_response(response.json())
            except (httpx.HTTPError, KeyError, ValueError):
                vectors = self._embed_with_legacy_api(client, texts)
        for vector in vectors:
            if len(vector) != self.dim:
                raise ValueError(f"expected vector length {self.dim}, got {len(vector)}")
        return vectors

    def _embed_with_legacy_api(self, client: httpx.Client, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            response = client.post("/api/embeddings", json={"model": self.model, "prompt": text})
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding")
            if not isinstance(embedding, list):
                raise ValueError("legacy ollama embedding payload missing embedding")
            vectors.append([float(item) for item in embedding])
        return vectors

    @staticmethod
    def _parse_embed_response(payload: dict[str, Any]) -> list[list[float]]:
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise ValueError("ollama embedding payload missing embeddings")
        normalized: list[list[float]] = []
        for item in embeddings:
            if not isinstance(item, list):
                raise ValueError("ollama embedding payload contains invalid vector")
            normalized.append([float(value) for value in item])
        return normalized
