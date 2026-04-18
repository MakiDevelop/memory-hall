from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Protocol

from memory_hall.models import SearchCandidate


class VectorStore(Protocol):
    dim: int

    def open(self) -> None: ...

    def close(self) -> None: ...

    def healthcheck(self) -> None: ...

    def upsert(self, tenant_id: str, entry_id: str, vec: list[float]) -> None: ...

    def search(self, tenant_id: str, query_vec: list[float], k: int) -> list[SearchCandidate]: ...

    def contains(self, tenant_id: str, entry_id: str) -> bool: ...

    def delete(self, tenant_id: str, entry_id: str) -> None: ...


class SqliteVecStore:
    def __init__(self, database_path: Path, dim: int = 1024) -> None:
        self.database_path = database_path
        self.dim = dim
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        self._apply_pragmas(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vectors (
                tenant_id TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, entry_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vectors_tenant ON vectors(tenant_id)"
        )
        connection.commit()
        self._connection = connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def healthcheck(self) -> None:
        connection = self._require_connection()
        connection.execute("SELECT 1").fetchone()

    def upsert(self, tenant_id: str, entry_id: str, vec: list[float]) -> None:
        self._validate_vector(vec)
        connection = self._require_connection()
        connection.execute(
            """
            INSERT INTO vectors (tenant_id, entry_id, vector_json)
            VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, entry_id)
            DO UPDATE SET vector_json = excluded.vector_json
            """,
            (tenant_id, entry_id, json.dumps(vec)),
        )
        connection.commit()

    def search(self, tenant_id: str, query_vec: list[float], k: int) -> list[SearchCandidate]:
        self._validate_vector(query_vec)
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT entry_id, vector_json FROM vectors WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            score = self._cosine_similarity(query_vec, json.loads(row["vector_json"]))
            scored.append((row["entry_id"], score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return [SearchCandidate(entry_id=entry_id, score=score) for entry_id, score in scored[:k]]

    def contains(self, tenant_id: str, entry_id: str) -> bool:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT 1 FROM vectors WHERE tenant_id = ? AND entry_id = ?",
            (tenant_id, entry_id),
        ).fetchone()
        return row is not None

    def delete(self, tenant_id: str, entry_id: str) -> None:
        connection = self._require_connection()
        connection.execute(
            "DELETE FROM vectors WHERE tenant_id = ? AND entry_id = ?",
            (tenant_id, entry_id),
        )
        connection.commit()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("vector store is not open")
        return self._connection

    def _validate_vector(self, vec: list[float]) -> None:
        if len(vec) != self.dim:
            raise ValueError(f"expected vector length {self.dim}, got {len(vec)}")

    @staticmethod
    def _apply_pragmas(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL;")
        connection.execute("PRAGMA synchronous=NORMAL;")
        connection.execute("PRAGMA busy_timeout=5000;")

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return numerator / (left_norm * right_norm)
