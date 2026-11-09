"""Local, single-project storage for the PM context example."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

CONTEXT_BYTES = 8_000
SECTION_BYTES = 1_600
SOURCE_BYTES = 2 * 1024 * 1024
RESULT_LIMIT = 6
APPLICATION_ID = 1296577329
CJK = re.compile(r"([\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]+)")
HEADING = re.compile(r"^#{1,6}\s+(.+)$")
STATUS = {"open", "blocked", "done"}
SHA256 = re.compile(r"[0-9a-fA-F]{64}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS store_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    path TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    current_sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    section_count INTEGER NOT NULL,
    PRIMARY KEY (path, sha256)
);
CREATE TABLE IF NOT EXISTS sections (
    section_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    heading TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS section_fts USING fts5(section_id UNINDEXED, terms);
CREATE TABLE IF NOT EXISTS contexts (
    context_id TEXT PRIMARY KEY,
    source_generation INTEGER NOT NULL,
    remaining_bytes INTEGER NOT NULL CHECK (remaining_bytes >= 0)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    summary TEXT NOT NULL,
    next_step TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);
CREATE TABLE IF NOT EXISTS task_heads (
    task_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);
"""

REMOTE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS source_texts (
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content BLOB NOT NULL,
        PRIMARY KEY (path, sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS remote_write_receipts (
        actor TEXT NOT NULL,
        operation TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        receipt TEXT NOT NULL,
        PRIMARY KEY (actor, operation, idempotency_key)
    )
    """,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _bounded(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required.")
    try:
        size = len(value.encode())
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8.") from None
    if size > limit:
        raise ValueError(f"{field} exceeds {limit} UTF-8 bytes.")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise ValueError(f"{field} contains unsupported control characters.")
    return value


def _prefix(value: str, byte_limit: int) -> str:
    return value.encode()[:byte_limit].decode(errors="ignore")


def _source_path(path: Path) -> str:
    if path.is_absolute():
        raise ValueError("Source path must be relative.")
    if not path.parts or any(part.startswith(".") for part in path.parts):
        raise ValueError("Hidden and parent paths are outside the source allowlist.")
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("Source must be a UTF-8 Markdown or text file.")
    return path.as_posix()


def _read_receipt(
    db: sqlite3.Connection, actor: str, operation: str, key: str, request_hash: str
) -> dict | None:
    row = db.execute(
        """
        SELECT request_hash, receipt FROM remote_write_receipts
        WHERE actor = ? AND operation = ? AND idempotency_key = ?
        """,
        (actor, operation, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise ValueError("Idempotency key already has different write data.")
    return json.loads(row["receipt"])


def _write_receipt(
    db: sqlite3.Connection, actor: str, operation: str, key: str, request_hash: str, payload: dict
) -> dict:
    receipt = {**payload, "actor": actor}
    db.execute(
        "INSERT INTO remote_write_receipts VALUES (?, ?, ?, ?, ?)",
        (actor, operation, key, request_hash, _json_bytes(receipt).decode()),
    )
    return receipt


def _terms(text: str, *, indexing: bool = False) -> list[str]:
    terms = []
    for part in CJK.split(unicodedata.normalize("NFKC", text).casefold()):
        if not part:
            continue
        if CJK.fullmatch(part):
            if indexing or len(part) == 1:
                terms.extend(part)
            terms.extend(a + b for a, b in zip(part, part[1:], strict=False))
        else:
            terms.extend(re.findall(r"[^\W_]+", part))
    return list(dict.fromkeys(terms))


def _sections(text: str) -> Iterator[tuple[str, int, int, str]]:
    heading = ""
    start = 1
    end = 1
    pieces: list[str] = []
    size = 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        match = HEADING.match(line)
        if match:
            if pieces:
                yield heading, start, end, "".join(pieces)
            heading = _prefix(match[1].strip(), 100)
            pieces = []
            size = 0
        raw = line.encode()
        offset = 0
        while offset < len(raw):
            if not pieces:
                start = number
            piece = raw[offset : offset + SECTION_BYTES - size].decode(errors="ignore")
            if not piece:
                yield heading, start, end, "".join(pieces)
                pieces = []
                size = 0
                continue
            piece_bytes = len(piece.encode())
            pieces.append(piece)
            end = number
            size += piece_bytes
            offset += piece_bytes
    if pieces:
        yield heading, start, end, "".join(pieces)


class Store:
    def __init__(self, db_path: Path, sources_root: Path):
        self.db_path = db_path.expanduser().resolve()
        try:
            self.sources_root = sources_root.expanduser().resolve(strict=True)
        except OSError:
            raise ValueError("The configured sources directory must exist.") from None
        if not self.sources_root.is_dir():
            raise ValueError("The configured sources directory must exist.")
        with self._transaction() as db:
            application_id = db.execute("PRAGMA application_id").fetchone()[0]
            if application_id == 0 and db.execute("SELECT 1 FROM sqlite_schema").fetchone() is None:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA application_id = 1296577329")
                db.execute(
                    "INSERT INTO store_config VALUES ('sources_root', ?)",
                    (str(self.sources_root),),
                )
                db.execute("INSERT INTO store_config VALUES ('source_generation', '0')")
            elif application_id != APPLICATION_ID:
                raise ValueError("Refusing to open a database owned by another application.")
            configured = db.execute(
                "SELECT value FROM store_config WHERE key = 'sources_root'"
            ).fetchone()
            if configured is None or configured[0] != str(self.sources_root):
                raise ValueError("This database belongs to a different sources directory.")
            for statement in REMOTE_SCHEMA:
                db.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _read_source(self, path: str) -> tuple[str, bytes]:
        relative = Path(_bounded(path, "path", 400))
        if relative.is_absolute():
            try:
                relative = relative.relative_to(self.sources_root)
            except ValueError:
                raise ValueError("Source path must be inside the configured directory.") from None
        relative_key = _source_path(relative)
        try:
            with ExitStack() as stack:
                directory = os.open(self.sources_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, directory)
                for part in relative.parts[:-1]:
                    directory = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    stack.callback(os.close, directory)
                descriptor = os.open(
                    relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise ValueError("Source must be a regular file.")
                    data = file.read(SOURCE_BYTES + 1)
        except OSError:
            raise ValueError("Unable to read a regular source file from the allowlist.") from None
        if len(data) > SOURCE_BYTES:
            raise ValueError("Source exceeds the 2 MiB pilot limit.")
        return relative_key, data

    def ingest_source(self, path: str) -> dict:
        with self._transaction() as db:
            relative, data = self._read_source(path)
            digest = hashlib.sha256(data).hexdigest()
            return self._index_source(
                db, relative, data, digest, self._current_source(db, relative)
            )

    @staticmethod
    def _current_source(db: sqlite3.Connection, relative: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT source_id, current_sha FROM sources WHERE path = ?", (relative,)
        ).fetchone()

    def source_status(self, path: str) -> dict:
        relative = _source_path(Path(_bounded(path, "path", 400)))
        with closing(self._connect()) as db:
            source = self._current_source(db, relative)
        return {"status": "ok", "path": relative, "sha256": source["current_sha"] if source else ""}

    def ingest_text(
        self,
        path: str,
        text: str,
        expected_sha256: str,
        idempotency_key: str,
        *,
        actor: str,
    ) -> dict:
        relative = _source_path(Path(_bounded(path, "path", 400)))
        data = _bounded(text, "text", SOURCE_BYTES).encode()
        _bounded(actor, "actor", 96)
        _bounded(idempotency_key, "idempotency_key", 96)
        if not isinstance(expected_sha256, str) or (
            expected_sha256 and not SHA256.fullmatch(expected_sha256)
        ):
            raise ValueError("expected_sha256 must be empty or a 64-digit SHA-256.")
        expected_sha256 = expected_sha256.lower()
        digest = hashlib.sha256(data).hexdigest()
        request_hash = hashlib.sha256(_json_bytes([relative, digest, expected_sha256])).hexdigest()
        with self._transaction() as db:
            receipt = _read_receipt(db, actor, "ingest_text", idempotency_key, request_hash)
            if receipt is not None:
                return receipt
            source = self._current_source(db, relative)
            current = source["current_sha"] if source else ""
            if current != expected_sha256:
                raise ValueError("Source revision conflict. Read source_status before updating.")
            payload = self._index_source(db, relative, data, digest, source)
            db.execute(
                "INSERT INTO source_texts VALUES (?, ?, ?) ON CONFLICT(path, sha256) DO NOTHING",
                (relative, digest, data),
            )
            return _write_receipt(db, actor, "ingest_text", idempotency_key, request_hash, payload)

    @staticmethod
    def _index_source(
        db: sqlite3.Connection, relative: str, data: bytes, digest: str, source: sqlite3.Row | None
    ) -> dict:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Source must be valid UTF-8.") from None
        if not text.strip() or "\0" in text:
            raise ValueError("Source must contain plain text.")
        source_id = source["source_id"] if source else uuid.uuid4().hex
        unchanged = source is not None and source["current_sha"] == digest
        previous = db.execute(
            "SELECT section_count FROM revisions WHERE path = ? AND sha256 = ?",
            (relative, digest),
        ).fetchone()
        if previous is None:
            rows = [
                (
                    hashlib.sha256(_json_bytes([relative, digest, index])).hexdigest(),
                    relative,
                    digest,
                    heading,
                    start,
                    end,
                    content,
                )
                for index, (heading, start, end, content) in enumerate(_sections(text))
                if content.strip()
            ]
            db.executemany("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            db.executemany(
                "INSERT INTO section_fts (section_id, terms) VALUES (?, ?)",
                (
                    (section_id, " ".join(_terms(f"{name} {heading} {body}", indexing=True)))
                    for section_id, name, _, heading, _, _, body in rows
                ),
            )
            count = len(rows)
            db.execute("INSERT INTO revisions VALUES (?, ?, ?)", (relative, digest, count))
        else:
            count = previous["section_count"]
        db.execute(
            """
            INSERT INTO sources VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET current_sha = excluded.current_sha
            """,
            (relative, source_id, digest),
        )
        if not unchanged:
            db.execute(
                """
                UPDATE store_config SET value = CAST(value AS INTEGER) + 1
                WHERE key = 'source_generation'
                """
            )
        return {
            "status": "indexed",
            "source_id": source_id,
            "path": relative,
            "sha256": digest,
            "sections": count,
            "unchanged": unchanged,
        }

    @staticmethod
    def _context(db: sqlite3.Connection, context_id: str) -> tuple[int, bool]:
        _bounded(context_id, "context_id", 32)
        row = db.execute(
            """
            SELECT remaining_bytes, source_generation != CAST(value AS INTEGER) AS stale
            FROM contexts CROSS JOIN store_config
            WHERE context_id = ? AND key = 'source_generation'
            """,
            (context_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown context_id. Call boot first.")
        return row["remaining_bytes"], bool(row["stale"])

    @staticmethod
    def _charge(db: sqlite3.Connection, context_id: str, remaining: int, payload: dict) -> dict:
        # Use the current digit count so the final remainder can never make the
        # emitted JSON larger than its reserved byte count.
        reserved = len(_json_bytes({**payload, "remaining_bytes": remaining}))
        if reserved > remaining:
            return {"status": "budget_exhausted", "remaining_bytes": remaining}
        remaining -= reserved
        db.execute(
            "UPDATE contexts SET remaining_bytes = ? WHERE context_id = ?",
            (remaining, context_id),
        )
        return {**payload, "remaining_bytes": remaining}

    def boot(self, task_id: str) -> dict:
        _bounded(task_id, "task_id", 96)
        with self._transaction() as db:
            context_id = uuid.uuid4().hex
            generation = int(
                db.execute(
                    "SELECT value FROM store_config WHERE key = 'source_generation'"
                ).fetchone()[0]
            )
            db.execute(
                "INSERT INTO contexts VALUES (?, ?, ?)", (context_id, generation, CONTEXT_BYTES)
            )
            state = db.execute(
                """
                SELECT c.task_id, c.revision, c.summary, c.next_step, c.status
                FROM task_heads h JOIN checkpoints c USING (task_id, revision)
                WHERE h.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            result = {
                "status": "ok",
                "context_id": context_id,
                "task_id": task_id,
                "source_generation": generation,
                "checkpoint": dict(state) if state else None,
                "revision": state["revision"] if state else 0,
            }
            if task_id == "project":
                tasks = db.execute(
                    """
                    SELECT c.task_id, c.revision, c.summary, c.next_step, c.status
                    FROM task_heads h JOIN checkpoints c USING (task_id, revision)
                    WHERE h.task_id != 'project' AND c.status IN ('open', 'blocked')
                    ORDER BY (c.status = 'blocked') DESC, c.task_id LIMIT 6
                    """
                ).fetchall()
                result["tasks"] = [
                    {
                        **dict(task),
                        "summary": _prefix(task["summary"], 110),
                        "next_step": _prefix(task["next_step"], 110),
                    }
                    for task in tasks[:5]
                ]
                result["has_more"] = len(tasks) > 5
            return self._charge(db, context_id, CONTEXT_BYTES, result)

    def search(self, context_id: str, query: str) -> dict:
        tokens = _terms(_bounded(query, "query", 300))
        if not tokens or len(tokens) > 24:
            raise ValueError("Query must contain 1 to 24 terms. Use short keywords.")
        match = " AND ".join(f'"{token}"' for token in tokens)
        with self._transaction() as db:
            remaining, stale = self._context(db, context_id)
            if stale:
                return {"status": "context_stale", "remaining_bytes": remaining}
            rows = db.execute(
                """
                SELECT s.section_id, s.path, s.sha256, s.heading, s.line_start, s.line_end, s.text
                FROM section_fts
                JOIN sections s USING (section_id)
                JOIN sources current ON current.path = s.path AND current.current_sha = s.sha256
                WHERE section_fts MATCH ?
                ORDER BY bm25(section_fts), s.path, s.line_start, s.section_id
                LIMIT ?
                """,
                (match, RESULT_LIMIT + 1),
            ).fetchall()
            results: list[dict] = []
            payload = {"status": "ok", "results": results, "has_more": bool(rows)}
            for row in rows[:RESULT_LIMIT]:
                item = dict(row)
                item["preview"] = _prefix(item.pop("text"), 220)
                results.append(item)
                payload["has_more"] = len(results) < len(rows)
                if len(_json_bytes({**payload, "remaining_bytes": remaining})) > remaining:
                    results.pop()
                    payload["has_more"] = True
                    break
            if rows and not results:
                return {"status": "budget_exhausted", "remaining_bytes": remaining}
            return self._charge(db, context_id, remaining, payload)

    def get(self, context_id: str, section_id: str) -> dict:
        _bounded(section_id, "section_id", 64)
        with self._transaction() as db:
            remaining, stale = self._context(db, context_id)
            if stale:
                return {"status": "context_stale", "remaining_bytes": remaining}
            row = db.execute(
                """
                SELECT s.*, (s.sha256 != current.current_sha) AS historical
                FROM sections s JOIN sources current USING (path)
                WHERE s.section_id = ?
                """,
                (section_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown section_id.")
            section = dict(row)
            section["historical"] = bool(section["historical"])
            return self._charge(db, context_id, remaining, {"status": "ok", "section": section})

    def checkpoint(
        self,
        task_id: str,
        summary: str,
        next_step: str,
        status: str,
        expected_revision: int,
        idempotency_key: str,
        *,
        actor: str = "local",
    ) -> dict:
        _bounded(task_id, "task_id", 96)
        _bounded(summary, "summary", 1_200)
        _bounded(next_step, "next_step", 400)
        _bounded(idempotency_key, "idempotency_key", 96)
        _bounded(actor, "actor", 96)
        if not isinstance(status, str) or status not in STATUS:
            raise ValueError("status must be open, blocked, or done.")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer.")
        digest = hashlib.sha256(
            _json_bytes([task_id, summary, next_step, status, expected_revision])
        ).hexdigest()
        with self._transaction() as db:
            storage_key = idempotency_key
            if actor != "local":
                receipt = _read_receipt(db, actor, "checkpoint", idempotency_key, digest)
                if receipt is not None:
                    return receipt
                # The longer key cannot collide with validated legacy local keys.
                storage_key = hashlib.sha512(_json_bytes([actor, idempotency_key])).hexdigest()
            existing = db.execute(
                "SELECT revision, request_hash FROM checkpoints WHERE idempotency_key = ?",
                (storage_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise ValueError("Idempotency key already has different checkpoint data.")
                return {"status": "saved", "task_id": task_id, "revision": existing["revision"]}
            head = db.execute(
                "SELECT revision FROM task_heads WHERE task_id = ?", (task_id,)
            ).fetchone()
            current = head["revision"] if head else 0
            if current != expected_revision:
                raise ValueError(f"Checkpoint revision conflict. Current revision: {current}.")
            revision = current + 1
            db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, revision, summary, next_step, status, storage_key, digest),
            )
            db.execute(
                """
                INSERT INTO task_heads VALUES (?, ?)
                ON CONFLICT(task_id) DO UPDATE SET revision = excluded.revision
                """,
                (task_id, revision),
            )
            result = {"status": "saved", "task_id": task_id, "revision": revision}
            if actor != "local":
                return _write_receipt(db, actor, "checkpoint", idempotency_key, digest, result)
            return result
