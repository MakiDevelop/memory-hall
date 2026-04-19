# ruff: noqa: S608

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import jieba

from memory_hall.models import Entry, InsertOutcome, SearchCandidate, decode_cursor, dump_json


class SqliteStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._writer_connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        await self._open_writer_connection()
        async with self._read_connection() as connection:
            await self._create_schema(connection)

    async def close(self) -> None:
        if self._writer_connection is not None:
            await self._writer_connection.close()
            self._writer_connection = None

    async def healthcheck(self) -> None:
        async with self._read_connection() as connection:
            await connection.execute("SELECT 1")

    async def insert_entry(self, entry: Entry) -> InsertOutcome:
        connection = await self._require_writer_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.execute(
                """
                INSERT INTO entries (
                    entry_id, tenant_id, agent_id, namespace, type, content, content_hash,
                    summary, tags_json, references_json, metadata_json, sync_status,
                    last_embedded_at, created_at, created_by_principal
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.entry_id,
                    entry.tenant_id,
                    entry.agent_id,
                    entry.namespace,
                    entry.type,
                    entry.content,
                    entry.content_hash,
                    entry.summary,
                    dump_json(entry.tags),
                    dump_json(entry.references),
                    dump_json(entry.metadata),
                    entry.sync_status,
                    entry.last_embedded_at.isoformat() if entry.last_embedded_at else None,
                    entry.created_at.isoformat(),
                    entry.created_by_principal,
                ),
            )
            await connection.execute(
                """
                INSERT INTO entries_fts (entry_id, tenant_id, content, summary, tags)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry.entry_id, entry.tenant_id, *self._build_fts_document(entry)),
            )
            await connection.commit()
            return InsertOutcome(entry=entry, created=True)
        except sqlite3.IntegrityError as exc:
            await connection.rollback()
            if (
                "entries.tenant_id, entries.content_hash" not in str(exc)
                and "UNIQUE" not in str(exc)
            ):
                raise
            existing = await self.get_entry_by_hash(entry.tenant_id, entry.content_hash)
            if existing is None:
                raise
            return InsertOutcome(entry=existing, created=False)

    async def update_sync_status(
        self,
        tenant_id: str,
        entry_id: str,
        sync_status: str,
        last_embedded_at: datetime | None,
    ) -> Entry | None:
        connection = await self._require_writer_connection()
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute(
            """
            UPDATE entries
            SET sync_status = ?, last_embedded_at = ?
            WHERE tenant_id = ? AND entry_id = ?
            """,
            (
                sync_status,
                last_embedded_at.isoformat() if last_embedded_at else None,
                tenant_id,
                entry_id,
            ),
        )
        await connection.commit()
        return await self.get_entry(tenant_id, entry_id)

    async def get_entry(self, tenant_id: str, entry_id: str) -> Entry | None:
        async with self._read_connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM entries WHERE tenant_id = ? AND entry_id = ?",
                (tenant_id, entry_id),
            )
            row = await cursor.fetchone()
            return self._row_to_entry(row) if row else None

    async def get_entry_by_hash(self, tenant_id: str, content_hash: str) -> Entry | None:
        async with self._read_connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM entries WHERE tenant_id = ? AND content_hash = ?",
                (tenant_id, content_hash),
            )
            row = await cursor.fetchone()
            return self._row_to_entry(row) if row else None

    async def get_entries_by_ids(self, tenant_id: str, entry_ids: list[str]) -> list[Entry]:
        if not entry_ids:
            return []
        placeholders = ",".join("?" for _ in entry_ids)
        async with self._read_connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT * FROM entries
                WHERE tenant_id = ? AND entry_id IN ({placeholders})
                """,
                (tenant_id, *entry_ids),
            )
            rows = await cursor.fetchall()
        mapping = {row["entry_id"]: self._row_to_entry(row) for row in rows}
        return [mapping[entry_id] for entry_id in entry_ids if entry_id in mapping]

    async def list_entries(
        self,
        tenant_id: str,
        *,
        namespaces: list[str] | None = None,
        agent_id: str | None = None,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[Entry]:
        conditions = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        self._apply_common_filters(
            conditions=conditions,
            params=params,
            alias="entries",
            namespaces=namespaces,
            agent_id=agent_id,
            types=types,
            tags=tags,
            since=since,
            until=until,
            cursor=cursor,
        )
        sql = "SELECT * FROM entries WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC, entry_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        async with self._read_connection() as connection:
            cursor_obj = await connection.execute(sql, params)
            rows = await cursor_obj.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def search_lexical(
        self,
        tenant_id: str,
        query: str,
        *,
        namespaces: list[str] | None = None,
        agent_id: str | None = None,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[SearchCandidate]:
        conditions = ["e.tenant_id = ?"]
        params: list[Any] = [tenant_id]
        self._apply_common_filters(
            conditions=conditions,
            params=params,
            alias="e",
            namespaces=namespaces,
            agent_id=agent_id,
            types=types,
            tags=tags,
            since=None,
            until=None,
            cursor=None,
        )
        normalized_query = self._normalize_fts_query(query)
        if not normalized_query:
            return []
        params.insert(0, normalized_query)
        params.append(limit)
        sql = """
            SELECT e.entry_id, bm25(entries_fts) AS bm25_score
            FROM entries_fts
            JOIN entries AS e
              ON e.entry_id = entries_fts.entry_id
             AND e.tenant_id = entries_fts.tenant_id
            WHERE entries_fts MATCH ?
              AND
        """
        sql += " AND ".join(conditions)
        sql += " ORDER BY bm25_score LIMIT ?"
        async with self._read_connection() as connection:
            cursor_obj = await connection.execute(sql, params)
            rows = await cursor_obj.fetchall()
        return [
            SearchCandidate(entry_id=row["entry_id"], score=self._normalize_bm25(row["bm25_score"]))
            for row in rows
        ]

    async def add_reference(
        self,
        tenant_id: str,
        source_entry_id: str,
        target_entry_id: str,
    ) -> Entry | None:
        source = await self.get_entry(tenant_id, source_entry_id)
        target = await self.get_entry(tenant_id, target_entry_id)
        if source is None or target is None:
            return None
        references = list(source.references)
        if target_entry_id not in references:
            references.append(target_entry_id)
        connection = await self._require_writer_connection()
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute(
            """
            UPDATE entries
            SET references_json = ?
            WHERE tenant_id = ? AND entry_id = ?
            """,
            (dump_json(references), tenant_id, source_entry_id),
        )
        await connection.commit()
        return await self.get_entry(tenant_id, source_entry_id)

    async def list_pending_entries(self, tenant_id: str, limit: int | None = None) -> list[Entry]:
        sql = "SELECT * FROM entries WHERE tenant_id = ? AND sync_status != 'embedded'"
        params: list[Any] = [tenant_id]
        sql += " ORDER BY created_at ASC, entry_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        async with self._read_connection() as connection:
            cursor = await connection.execute(sql, params)
            rows = await cursor.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def get_references_out(self, tenant_id: str, entry_id: str) -> list[Entry]:
        async with self._read_connection() as connection:
            cursor = await connection.execute(
                """
                SELECT e.*
                FROM entries AS e
                JOIN json_each(
                    COALESCE(
                        (
                            SELECT references_json
                            FROM entries
                            WHERE tenant_id = ? AND entry_id = ?
                        ),
                        '[]'
                    )
                ) AS refs
                  ON refs.value = e.entry_id
                WHERE e.tenant_id = ?
                ORDER BY e.created_at DESC, e.entry_id DESC
                """,
                (tenant_id, entry_id, tenant_id),
            )
            rows = await cursor.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def get_references_in(self, tenant_id: str, entry_id: str) -> list[Entry]:
        async with self._read_connection() as connection:
            cursor = await connection.execute(
                """
                SELECT DISTINCT e.*
                FROM entries AS e
                JOIN json_each(COALESCE(e.references_json, '[]')) AS refs
                  ON refs.value = ?
                WHERE e.tenant_id = ?
                ORDER BY e.created_at DESC, e.entry_id DESC
                """,
                (entry_id, tenant_id),
            )
            rows = await cursor.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def audit(self) -> dict[str, object]:
        async with self._read_connection() as connection:
            total_entries = await self._fetch_count(connection, "SELECT COUNT(*) FROM entries")
            tenant_counts = await self._fetch_key_count(
                connection,
                """
                SELECT tenant_id, COUNT(*) AS count
                FROM entries
                GROUP BY tenant_id
                ORDER BY tenant_id
                """,
            )
            namespace_counts = await self._fetch_key_count(
                connection,
                """
                SELECT namespace, COUNT(*) AS count
                FROM entries
                GROUP BY namespace
                ORDER BY namespace
                """,
            )
            sync_status_counts = await self._fetch_key_count(
                connection,
                """
                SELECT sync_status, COUNT(*) AS count
                FROM entries
                GROUP BY sync_status
                ORDER BY sync_status
                """,
            )
            collisions = await self._fetch_count(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT tenant_id, content_hash
                    FROM entries
                    GROUP BY tenant_id, content_hash
                    HAVING COUNT(*) > 1
                )
                """,
            )
        return {
            "total_entries": total_entries,
            "tenant_counts": tenant_counts,
            "namespace_counts": namespace_counts,
            "sync_status_counts": sync_status_counts,
            "content_hash_collisions": collisions,
        }

    async def reindex_fts_entries(self, entries: list[Entry]) -> int:
        if not entries:
            return 0
        connection = await self._require_writer_connection()
        await connection.execute("BEGIN IMMEDIATE")
        try:
            reindexed = 0
            for entry in entries:
                reindexed += await self._refresh_fts_row(connection, entry)
            await connection.commit()
            return reindexed
        except Exception:
            await connection.rollback()
            raise

    async def _open_writer_connection(self) -> None:
        self._writer_connection = await aiosqlite.connect(self.database_path)
        self._writer_connection.row_factory = aiosqlite.Row
        await self._apply_pragmas(self._writer_connection)
        await self._create_schema(self._writer_connection)

    async def _create_schema(self, connection: aiosqlite.Connection) -> None:
        await connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                entry_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                summary TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                references_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                sync_status TEXT NOT NULL DEFAULT 'pending',
                last_embedded_at TEXT,
                created_at TEXT NOT NULL,
                created_by_principal TEXT NOT NULL,
                UNIQUE (tenant_id, content_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_tenant_created
            ON entries(tenant_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_tenant_agent
            ON entries(tenant_id, agent_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_tenant_ns
            ON entries(tenant_id, namespace, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_tenant_type
            ON entries(tenant_id, type, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_sync_pending
            ON entries(sync_status)
            WHERE sync_status != 'embedded';

            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                entry_id UNINDEXED,
                tenant_id UNINDEXED,
                content,
                summary,
                tags,
                tokenize='unicode61 remove_diacritics 0'
            );
            """
        )
        await connection.commit()

    async def _require_writer_connection(self) -> aiosqlite.Connection:
        if self._writer_connection is None:
            raise RuntimeError("writer connection is not open")
        return self._writer_connection

    @asynccontextmanager
    async def _read_connection(self):
        connection = await aiosqlite.connect(self.database_path)
        try:
            await self._apply_pragmas(connection)
            yield connection
        finally:
            await connection.close()

    @staticmethod
    async def _apply_pragmas(connection: aiosqlite.Connection) -> None:
        await connection.execute("PRAGMA journal_mode=WAL;")
        await connection.execute("PRAGMA synchronous=NORMAL;")
        await connection.execute("PRAGMA busy_timeout=5000;")
        connection.row_factory = aiosqlite.Row

    @staticmethod
    def _row_to_entry(row: aiosqlite.Row | sqlite3.Row) -> Entry:
        return Entry(
            entry_id=row["entry_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            namespace=row["namespace"],
            type=row["type"],
            content=row["content"],
            content_hash=row["content_hash"],
            summary=row["summary"],
            tags=json.loads(row["tags_json"] or "[]"),
            references=json.loads(row["references_json"] or "[]"),
            metadata=json.loads(row["metadata_json"] or "{}"),
            sync_status=row["sync_status"],
            last_embedded_at=datetime.fromisoformat(row["last_embedded_at"])
            if row["last_embedded_at"]
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by_principal=row["created_by_principal"],
        )

    @classmethod
    def _apply_common_filters(
        cls,
        *,
        conditions: list[str],
        params: list[Any],
        alias: str,
        namespaces: list[str] | None,
        agent_id: str | None,
        types: list[str] | None,
        tags: list[str] | None,
        since: datetime | None,
        until: datetime | None,
        cursor: str | None,
    ) -> None:
        if namespaces:
            placeholders = ",".join("?" for _ in namespaces)
            conditions.append(f"{alias}.namespace IN ({placeholders})")
            params.extend(namespaces)
        if agent_id:
            conditions.append(f"{alias}.agent_id = ?")
            params.append(agent_id)
        if types:
            placeholders = ",".join("?" for _ in types)
            conditions.append(f"{alias}.type IN ({placeholders})")
            params.extend(types)
        if tags:
            for tag in tags:
                conditions.append(
                    f"""
                    EXISTS (
                        SELECT 1
                        FROM json_each(COALESCE({alias}.tags_json, '[]'))
                        WHERE value = ?
                    )
                    """
                )
                params.append(tag)
        if since:
            conditions.append(f"{alias}.created_at >= ?")
            params.append(since.isoformat())
        if until:
            conditions.append(f"{alias}.created_at <= ?")
            params.append(until.isoformat())
        if cursor:
            created_at, entry_id = decode_cursor(cursor)
            conditions.append(
                f"({alias}.created_at < ? OR ({alias}.created_at = ? AND {alias}.entry_id < ?))"
            )
            params.extend([created_at.isoformat(), created_at.isoformat(), entry_id])

    @staticmethod
    def _normalize_fts_query(query: str) -> str:
        tokens = SqliteStore._tokenize_fts_text(query)
        return " AND ".join(f'"{token}"' for token in tokens if token)

    @staticmethod
    def _normalize_bm25(score: float) -> float:
        return 1.0 / (1.0 + abs(score))

    @classmethod
    def _build_fts_document(cls, entry: Entry) -> tuple[str, str, str]:
        return (
            cls._tokenize_fts_value(entry.content),
            cls._tokenize_fts_value(entry.summary or ""),
            cls._tokenize_fts_value(" ".join(entry.tags)),
        )

    @classmethod
    def _tokenize_fts_value(cls, text: str) -> str:
        return " ".join(cls._tokenize_fts_text(text))

    @classmethod
    def _tokenize_fts_text(cls, text: str) -> list[str]:
        tokens: list[str] = []
        for raw_token in jieba.cut(text):
            token = raw_token.replace('"', " ").strip()
            if not token or not any(char.isalnum() for char in token):
                continue
            tokens.append(token)
        seen = set(tokens)
        base_tokens = list(tokens)
        for left_token, right_token in zip(base_tokens, base_tokens[1:], strict=False):
            if cls._is_single_cjk_token(left_token) and cls._is_single_cjk_token(right_token):
                bigram = left_token + right_token
                if bigram not in seen:
                    tokens.append(bigram)
                    seen.add(bigram)
        return tokens

    @staticmethod
    def _is_single_cjk_token(token: str) -> bool:
        return len(token) == 1 and SqliteStore._is_cjk_char(token)

    @staticmethod
    def _is_cjk_char(char: str) -> bool:
        codepoint = ord(char)
        return (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        )

    async def _refresh_fts_row(self, connection: aiosqlite.Connection, entry: Entry) -> int:
        content, summary, tags = self._build_fts_document(entry)
        cursor = await connection.execute(
            """
            SELECT content, summary, tags
            FROM entries_fts
            WHERE tenant_id = ? AND entry_id = ?
            """,
            (entry.tenant_id, entry.entry_id),
        )
        rows = await cursor.fetchall()
        if len(rows) == 1 and (
            rows[0]["content"],
            rows[0]["summary"],
            rows[0]["tags"],
        ) == (content, summary, tags):
            return 0
        await connection.execute(
            "DELETE FROM entries_fts WHERE tenant_id = ? AND entry_id = ?",
            (entry.tenant_id, entry.entry_id),
        )
        await connection.execute(
            """
            INSERT INTO entries_fts (entry_id, tenant_id, content, summary, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry.entry_id, entry.tenant_id, content, summary, tags),
        )
        return 1

    @staticmethod
    async def _fetch_count(connection: aiosqlite.Connection, sql: str) -> int:
        cursor = await connection.execute(sql)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _fetch_key_count(connection: aiosqlite.Connection, sql: str) -> dict[str, int]:
        cursor = await connection.execute(sql)
        rows = await cursor.fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
