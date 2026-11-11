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
from datetime import UTC, date, datetime
from pathlib import Path

from budget import BudgetTooSmall, finalize
from migration import V1_APPLICATION_ID, V2_APPLICATION_ID, migrate
from safety import require_safe
from summaries import extractive_summary

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
    db: sqlite3.Connection, namespace: str, actor: str, operation: str, key: str, request_hash: str
) -> dict | None:
    row = db.execute(
        """
        SELECT request_hash, receipt FROM pm_write_receipts
        WHERE namespace = ? AND actor = ? AND operation = ? AND idempotency_key = ?
        """,
        (namespace, actor, operation, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise ValueError("Idempotency key already has different write data.")
    return json.loads(row["receipt"])


def _write_receipt(
    db: sqlite3.Connection,
    namespace: str,
    actor: str,
    operation: str,
    key: str,
    request_hash: str,
    payload: dict,
) -> dict:
    receipt = {**payload, "actor": actor}
    db.execute(
        "INSERT INTO pm_write_receipts"
        "(namespace,actor,operation,idempotency_key,request_hash,receipt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (namespace, actor, operation, key, request_hash, _json_bytes(receipt).decode()),
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _event_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("Date must use YYYY-MM-DD in Asia/Taipei.")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid calendar date.") from None
    return value


def _metadata_precondition(value):
    if value is not None and (type(value) is not int or not 0 <= value < 2**63):
        raise ValueError("expected_metadata_revision must be a nonnegative integer.")


from handoff import HandoffMixin  # noqa: E402


class Store(HandoffMixin):
    def __init__(
        self,
        db_path: Path,
        sources_root: Path,
        *,
        default_namespace="default",
        handoff_destinations=None,
    ):
        self.db_path = db_path.expanduser().resolve()
        self.default_namespace = self._namespace(default_namespace)
        self.handoff_destinations = handoff_destinations
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
                    "INSERT INTO store_config(key,value) VALUES ('sources_root',?)",
                    (str(self.sources_root),),
                )
                db.execute("INSERT INTO store_config(key,value) VALUES ('source_generation','0')")
            elif application_id not in (V1_APPLICATION_ID, V2_APPLICATION_ID):
                raise ValueError("Refusing to open a database owned by another application.")
            configured = db.execute(
                "SELECT value FROM store_config WHERE key='sources_root'"
            ).fetchone()
            if configured is None or configured[0] != str(self.sources_root):
                raise ValueError("This database belongs to a different sources directory.")
            for statement in REMOTE_SCHEMA:
                db.execute(statement)
            migrate(db, self.default_namespace)

    @staticmethod
    def _namespace(value: str) -> str:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", value) is None
        ):
            raise ValueError("Invalid namespace.")
        require_safe(value)
        return value

    def _scope(self, namespace, actor):
        _bounded(actor, "actor", 96)
        require_safe(actor)
        return self._namespace(self.default_namespace if namespace is None else namespace)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _ensure_namespace(db, namespace):
        db.execute(
            "INSERT INTO pm_namespaces(namespace,source_generation) VALUES (?,0) "
            "ON CONFLICT(namespace) DO NOTHING",
            (namespace,),
        )

    @staticmethod
    def _generation(db, namespace):
        row = db.execute(
            "SELECT source_generation FROM pm_namespaces WHERE namespace=?", (namespace,)
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _current_source(db, namespace, relative):
        return db.execute(
            "SELECT s.source_id,s.current_sha,r.metadata_revision FROM pm_sources s "
            "JOIN pm_revisions r ON r.namespace=s.namespace AND r.path=s.path "
            "AND r.sha256=s.current_sha WHERE s.namespace=? AND s.path=?",
            (namespace, relative),
        ).fetchone()

    _receipt_read = staticmethod(_read_receipt)
    _receipt_write = staticmethod(_write_receipt)

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

    @staticmethod
    def _metadata(
        data,
        *,
        summary=None,
        record_type="document",
        event_date=None,
        freshness="unverified",
        lifecycle="active",
        summary_kind="extractive",
        summary_model=None,
        summary_prompt_sha256=None,
    ):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Source must be valid UTF-8.") from None
        if not text.strip() or "\0" in text:
            raise ValueError("Source must contain plain text.")
        require_safe(text)
        if (
            not isinstance(record_type, str)
            or re.fullmatch(r"[a-z][a-z_]{0,39}", record_type) is None
        ):
            raise ValueError("Invalid record_type.")
        if freshness not in ("verified", "stale", "unverified"):
            raise ValueError("Invalid source freshness.")
        if lifecycle not in ("proposed", "active", "superseded", "revoked"):
            raise ValueError("Invalid decision lifecycle.")
        if summary is None:
            if summary_kind != "extractive":
                raise ValueError("A non-extractive summary must be supplied explicitly.")
            summary = extractive_summary(text)
        elif summary_kind == "extractive":
            # Only this method's generated prefix is an extractive summary.
            summary_kind = "manual"
        if summary_kind not in ("extractive", "manual", "model"):
            raise ValueError("Invalid summary_kind.")
        _bounded(summary, "summary", 1_600)
        if len(summary) > 300:
            raise ValueError("Summary exceeds 300 Unicode codepoints.")
        if summary_kind == "model" and not summary_model:
            raise ValueError("Model summary requires summary_model.")
        if summary_model is not None:
            _bounded(summary_model, "summary_model", 200)
        if summary_prompt_sha256 is not None and (
            not isinstance(summary_prompt_sha256, str)
            or not SHA256.fullmatch(summary_prompt_sha256)
        ):
            raise ValueError("Invalid summary prompt SHA-256.")
        event_date = _event_date(event_date)
        for value in (
            summary,
            record_type,
            event_date,
            freshness,
            lifecycle,
            summary_kind,
            summary_model,
            summary_prompt_sha256,
        ):
            if value is not None:
                require_safe(value)
        return {
            "record_type": record_type,
            "event_date": event_date,
            "freshness": freshness,
            "lifecycle": lifecycle,
            "summary": summary,
            "summary_kind": summary_kind,
            "summary_model": summary_model,
            "summary_prompt_sha256": summary_prompt_sha256,
            "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
        }

    def ingest_source(
        self,
        path,
        *,
        namespace=None,
        actor="local",
        summary=None,
        record_type="document",
        event_date=None,
        freshness="unverified",
        lifecycle="active",
        summary_kind="extractive",
        summary_model=None,
        summary_prompt_sha256=None,
        expected_metadata_revision=None,
    ):
        namespace = self._scope(namespace, actor)
        _metadata_precondition(expected_metadata_revision)
        relative, data = self._read_source(path)
        if relative.startswith("_handoffs/"):
            raise ValueError("The handoff source prefix is reserved.")
        require_safe(relative)
        metadata = self._metadata(
            data,
            summary=summary,
            record_type=record_type,
            event_date=event_date,
            freshness=freshness,
            lifecycle=lifecycle,
            summary_kind=summary_kind,
            summary_model=summary_model,
            summary_prompt_sha256=summary_prompt_sha256,
        )
        digest = hashlib.sha256(data).hexdigest()
        explicit = (
            summary is not None
            or record_type != "document"
            or event_date is not None
            or freshness != "unverified"
            or lifecycle != "active"
            or summary_kind != "extractive"
            or summary_model is not None
            or summary_prompt_sha256 is not None
        )
        with self._transaction() as db:
            return self._index_source(
                db,
                namespace,
                relative,
                data,
                digest,
                metadata,
                update_metadata=explicit,
                expected_metadata_revision=expected_metadata_revision,
            )

    def source_status(self, path, *, namespace=None, actor="local"):
        namespace = self._scope(namespace, actor)
        relative = _source_path(Path(_bounded(path, "path", 400)))
        require_safe(relative)
        with closing(self._connect()) as db:
            source = self._current_source(db, namespace, relative)
        return {
            "status": "ok",
            "namespace": namespace,
            "path": relative,
            "sha256": source["current_sha"] if source else "",
            "metadata_revision": source["metadata_revision"] if source else 0,
        }

    def ingest_text(
        self,
        path,
        text,
        expected_sha256,
        idempotency_key,
        *,
        actor="local",
        namespace=None,
        summary=None,
        record_type="document",
        event_date=None,
        freshness="unverified",
        lifecycle="active",
        summary_kind="extractive",
        summary_model=None,
        summary_prompt_sha256=None,
        expected_metadata_revision=None,
        allow_active_decisions=True,
    ):
        namespace = self._scope(namespace, actor)
        _metadata_precondition(expected_metadata_revision)
        relative = _source_path(Path(_bounded(path, "path", 400)))
        if relative.startswith("_handoffs/"):
            raise ValueError("The handoff source prefix is reserved.")
        require_safe(relative)
        data = _bounded(text, "text", SOURCE_BYTES).encode()
        _bounded(idempotency_key, "idempotency_key", 96)
        require_safe(idempotency_key)
        if not isinstance(expected_sha256, str) or (
            expected_sha256 and not SHA256.fullmatch(expected_sha256)
        ):
            raise ValueError("expected_sha256 must be empty or a 64-digit SHA-256.")
        expected_sha256 = expected_sha256.lower()
        metadata = self._metadata(
            data,
            summary=summary,
            record_type=record_type,
            event_date=event_date,
            freshness=freshness,
            lifecycle=lifecycle,
            summary_kind=summary_kind,
            summary_model=summary_model,
            summary_prompt_sha256=summary_prompt_sha256,
        )
        digest = hashlib.sha256(data).hexdigest()
        request = [relative, digest, expected_sha256]
        # Preserve the exact v1 replay hash when all optional arguments are omitted.
        explicit = (
            summary is not None
            or record_type != "document"
            or event_date is not None
            or freshness != "unverified"
            or lifecycle != "active"
            or summary_kind != "extractive"
            or summary_model is not None
            or summary_prompt_sha256 is not None
        )
        if explicit or expected_metadata_revision is not None:
            request.append(metadata)
        if expected_metadata_revision is not None:
            request.append(expected_metadata_revision)
        request_hash = hashlib.sha256(_json_bytes(request)).hexdigest()
        with self._transaction() as db:
            self._active_decision_write(
                db, namespace, relative, record_type, lifecycle, allow_active_decisions
            )
            receipt = _read_receipt(
                db, namespace, actor, "ingest_text", idempotency_key, request_hash
            )
            if receipt is not None:
                return receipt
            source = self._current_source(db, namespace, relative)
            current = source["current_sha"] if source else ""
            if current != expected_sha256:
                raise ValueError("Source revision conflict. Read source_status before updating.")
            payload = self._index_source(
                db,
                namespace,
                relative,
                data,
                digest,
                metadata,
                update_metadata=explicit,
                expected_metadata_revision=expected_metadata_revision,
            )
            payload["operation_id"] = uuid.uuid4().hex
            return _write_receipt(
                db, namespace, actor, "ingest_text", idempotency_key, request_hash, payload
            )

    @staticmethod
    def _active_decision_write(db, namespace, path, record_type, lifecycle, allowed):
        """Service-injected capability, checked under the same write transaction."""
        if type(allowed) is not bool:
            raise ValueError("allow_active_decisions must be a service-provided boolean.")
        if allowed:
            return
        if record_type in ("decision", "requirement") and lifecycle == "active":
            raise ValueError("PM capability required to publish active decisions or requirements.")
        protected = db.execute(
            """SELECT 1 FROM pm_sources s JOIN pm_revisions r
          ON r.namespace=s.namespace AND r.path=s.path AND r.sha256=s.current_sha
          WHERE s.namespace=? AND s.path=? AND r.lifecycle='active'
            AND r.record_type IN ('decision','requirement')""",
            (namespace, path),
        ).fetchone()
        if protected is not None:
            raise ValueError("PM capability required to change an active decision or requirement.")

    def model_ingest_text(
        self,
        path,
        text,
        expected_sha256,
        idempotency_key,
        *,
        summarizer,
        actor="local",
        namespace=None,
        record_type="document",
        event_date=None,
        freshness="unverified",
        lifecycle="active",
        expected_metadata_revision=None,
        allow_active_decisions=True,
    ):
        """Use a raw-request receipt before invoking a trusted configured summarizer.

        The transaction serializes equal retries and source/metadata CAS with the
        model call. The adapter must bound its own model timeout. Generated output
        and changing backend configuration never become the caller's request hash;
        a new intentional summarization requires a new idempotency key.
        """
        namespace = self._scope(namespace, actor)
        _metadata_precondition(expected_metadata_revision)
        relative = _source_path(Path(_bounded(path, "path", 400)))
        if relative.startswith("_handoffs/"):
            raise ValueError("The handoff source prefix is reserved.")
        require_safe(relative)
        data = _bounded(text, "text", SOURCE_BYTES).encode()
        _bounded(idempotency_key, "idempotency_key", 96)
        require_safe(idempotency_key)
        if not isinstance(expected_sha256, str) or (
            expected_sha256 and not SHA256.fullmatch(expected_sha256)
        ):
            raise ValueError("expected_sha256 must be empty or a 64-digit SHA-256.")
        expected_sha256 = expected_sha256.lower()
        # Validate and scan all raw metadata without generating a summary.
        checked = self._metadata(
            data,
            summary="Raw request validation",
            summary_kind="manual",
            record_type=record_type,
            event_date=event_date,
            freshness=freshness,
            lifecycle=lifecycle,
        )
        digest = hashlib.sha256(data).hexdigest()
        request = [
            "model-ingest-v1",
            relative,
            digest,
            expected_sha256,
            checked["record_type"],
            checked["event_date"],
            checked["freshness"],
            checked["lifecycle"],
            expected_metadata_revision,
        ]
        request_hash = hashlib.sha256(_json_bytes(request)).hexdigest()
        with self._transaction() as db:
            self._active_decision_write(
                db, namespace, relative, record_type, lifecycle, allow_active_decisions
            )
            receipt = _read_receipt(
                db, namespace, actor, "ingest_text", idempotency_key, request_hash
            )
            if receipt is not None:
                return receipt
            source = self._current_source(db, namespace, relative)
            current = source["current_sha"] if source else ""
            if current != expected_sha256:
                raise ValueError("Source revision conflict. Read source_status before updating.")
            if (
                source is not None
                and current == digest
                and (expected_metadata_revision != source["metadata_revision"])
            ):
                raise ValueError(
                    "Metadata revision conflict. Current metadata revision: "
                    f"{source['metadata_revision']}. Read source_status before updating."
                )
            if not callable(getattr(summarizer, "summarize", None)):
                raise ValueError("Model summarization is not configured.")
            generated = summarizer.summarize(text)
            if (
                not isinstance(generated, dict)
                or generated.get("summary_kind") != "model"
                or not isinstance(generated.get("prompt_sha256"), str)
                or not SHA256.fullmatch(generated["prompt_sha256"])
            ):
                raise ValueError(
                    "Model summarizer must return model summary and prompt provenance."
                )
            metadata = self._metadata(
                data,
                summary=generated.get("summary"),
                summary_kind="model",
                summary_model=generated.get("summary_model"),
                summary_prompt_sha256=generated["prompt_sha256"],
                record_type=record_type,
                event_date=event_date,
                freshness=freshness,
                lifecycle=lifecycle,
            )
            payload = self._index_source(
                db,
                namespace,
                relative,
                data,
                digest,
                metadata,
                expected_metadata_revision=expected_metadata_revision,
            )
            payload["operation_id"] = uuid.uuid4().hex
            return _write_receipt(
                db, namespace, actor, "ingest_text", idempotency_key, request_hash, payload
            )

    def _index_source(
        self,
        db,
        namespace,
        relative,
        data,
        digest,
        metadata,
        *,
        update_metadata=True,
        expected_metadata_revision=None,
    ):
        self._ensure_namespace(db, namespace)
        text = data.decode("utf-8-sig")
        source = self._current_source(db, namespace, relative)
        source_id = source["source_id"] if source else uuid.uuid4().hex
        unchanged = source is not None and source["current_sha"] == digest
        previous = db.execute(
            "SELECT * FROM pm_revisions WHERE namespace=? AND path=? AND sha256=?",
            (namespace, relative, digest),
        ).fetchone()
        metadata_changed = (
            previous is not None
            and update_metadata
            and any(previous[k] != v for k, v in metadata.items())
        )
        if (
            unchanged
            and metadata_changed
            and expected_metadata_revision != previous["metadata_revision"]
        ):
            raise ValueError(
                "Metadata revision conflict. Current metadata revision: "
                f"{previous['metadata_revision']}. Read source_status before updating."
            )
        if previous is None:
            rows = [
                (
                    hashlib.sha256(_json_bytes([namespace, relative, digest, index])).hexdigest(),
                    namespace,
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
            db.executemany(
                "INSERT INTO pm_sections"
                "(section_id,namespace,path,sha256,heading,line_start,line_end,text) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            db.executemany(
                "INSERT INTO section_fts(section_id,terms) VALUES (?,?)",
                (
                    (row[0], " ".join(_terms(f"{relative} {row[4]} {row[7]}", indexing=True)))
                    for row in rows
                ),
            )
            count = len(rows)
            db.execute(
                "INSERT INTO pm_revisions"
                "(namespace,path,sha256,section_count,record_type,freshness,lifecycle,imported_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    namespace,
                    relative,
                    digest,
                    count,
                    metadata["record_type"],
                    metadata["freshness"],
                    metadata["lifecycle"],
                    _now(),
                ),
            )
        else:
            count = previous["section_count"]
        if previous is None or metadata_changed:
            db.execute(
                """UPDATE pm_revisions SET record_type=?,event_date=?,freshness=?,lifecycle=?,
              summary=?,summary_kind=?,summary_model=?,summary_prompt_sha256=?,summary_sha256=?,
              summary_generated_at=?,metadata_revision=metadata_revision+?
              WHERE namespace=? AND path=? AND sha256=?""",
                tuple(
                    metadata[k]
                    for k in (
                        "record_type",
                        "event_date",
                        "freshness",
                        "lifecycle",
                        "summary",
                        "summary_kind",
                        "summary_model",
                        "summary_prompt_sha256",
                        "summary_sha256",
                    )
                )
                + (_now(), int(metadata_changed), namespace, relative, digest),
            )
        db.execute(
            "INSERT INTO pm_sources(namespace,path,source_id,current_sha) VALUES (?,?,?,?) "
            "ON CONFLICT(namespace,path) DO UPDATE SET current_sha=excluded.current_sha",
            (namespace, relative, source_id, digest),
        )
        db.execute(
            "INSERT INTO pm_source_texts(namespace,path,sha256,content) VALUES (?,?,?,?) "
            "ON CONFLICT(namespace,path,sha256) DO NOTHING",
            (namespace, relative, digest, data),
        )
        if not unchanged or metadata_changed:
            db.execute(
                "UPDATE pm_namespaces SET source_generation=source_generation+1 WHERE namespace=?",
                (namespace,),
            )
        return {
            "status": "indexed",
            "namespace": namespace,
            "source_id": source_id,
            "path": relative,
            "sha256": digest,
            "sections": count,
            "unchanged": unchanged,
            "metadata_changed": metadata_changed,
            "metadata_revision": previous["metadata_revision"] + int(metadata_changed)
            if previous
            else 1,
        }

    def context_namespace(self, context_id, *, actor="local"):
        _bounded(context_id, "context_id", 32)
        _bounded(actor, "actor", 96)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT namespace,actor FROM pm_contexts WHERE context_id=?", (context_id,)
            ).fetchone()
        if row is None or row["actor"] != actor:
            raise ValueError("Unknown or unauthorized context_id. Call boot first.")
        return row["namespace"]

    def _context(self, db, context_id, namespace, actor):
        _bounded(context_id, "context_id", 32)
        row = db.execute(
            "SELECT namespace,actor,source_generation,remaining_bytes "
            "FROM pm_contexts WHERE context_id=?",
            (context_id,),
        ).fetchone()
        if row is None or row["actor"] != actor or row["namespace"] != namespace:
            raise ValueError("Unknown or unauthorized context_id. Call boot first.")
        return row["remaining_bytes"], row["source_generation"] != self._generation(db, namespace)

    @staticmethod
    def _budget_error(remaining):
        return {
            "status": "budget_exhausted",
            "remaining_bytes": remaining,
            "remaining_context_bytes": remaining,
        }

    @staticmethod
    def _charge(db, context_id, remaining, payload, budget_bytes=None, truncated_count=0):
        result, charge = finalize(payload, budget_bytes, remaining, truncated_count=truncated_count)
        db.execute(
            "UPDATE pm_contexts SET remaining_bytes=? WHERE context_id=?",
            (remaining - charge, context_id),
        )
        return result

    def _fit_list(self, db, context_id, remaining, payload, field, budget_bytes=None, truncated=0):
        while True:
            try:
                return self._charge(db, context_id, remaining, payload, budget_bytes, truncated)
            except BudgetTooSmall:
                if payload[field]:
                    last = payload[field][-1]
                    if field == "results" and not last.get("expand"):
                        pointer = {
                            key: last[key]
                            for key in (
                                "section_id",
                                "path",
                                "sha256",
                                "heading",
                                "line_start",
                                "line_end",
                                "event_date",
                                "freshness",
                                "lifecycle",
                                "historical",
                            )
                        }
                        pointer["expand"] = True
                        pointer["omitted_bytes"] = sum(
                            len(last.get(key, "").encode())
                            for key in ("text", "summary", "preview")
                        )
                        payload[field][-1] = pointer
                        payload["has_more"] = True
                        truncated += 1
                        continue
                    payload[field].pop()
                    payload["has_more"] = True
                    if not last.get("expand"):
                        truncated += 1
                elif budget_bytes is None or remaining < budget_bytes:
                    return self._budget_error(remaining)
                else:
                    raise

    def boot(self, task_id, *, namespace=None, actor="local", budget_bytes=None):
        namespace = self._scope(namespace, actor)
        _bounded(task_id, "task_id", 96)
        require_safe(task_id)
        with self._transaction() as db:
            self._ensure_namespace(db, namespace)
            context_id = uuid.uuid4().hex
            generation = self._generation(db, namespace)
            db.execute(
                "INSERT INTO pm_contexts"
                "(context_id,namespace,actor,source_generation,remaining_bytes) VALUES (?,?,?,?,?)",
                (context_id, namespace, actor, generation, CONTEXT_BYTES),
            )
            state = db.execute(
                """SELECT c.task_id,c.revision,c.summary,c.next_step,c.status
              FROM pm_task_heads h JOIN pm_checkpoints c USING(namespace,task_id,revision)
              WHERE h.namespace=? AND h.task_id=?""",
                (namespace, task_id),
            ).fetchone()
            result = {
                "status": "ok",
                "context_id": context_id,
                "namespace": namespace,
                "task_id": task_id,
                "source_generation": generation,
                "checkpoint": dict(state) if state else None,
                "revision": state["revision"] if state else 0,
            }
            if task_id == "project":
                tasks = db.execute(
                    """SELECT c.task_id,c.revision,c.summary,c.next_step,c.status
                  FROM pm_task_heads h JOIN pm_checkpoints c USING(namespace,task_id,revision)
                  WHERE h.namespace=? AND h.task_id!='project' AND c.status IN ('open','blocked')
                  ORDER BY (c.status='blocked') DESC,c.task_id LIMIT 6""",
                    (namespace,),
                ).fetchall()
                result["tasks"] = [
                    {
                        **dict(t),
                        "summary": _prefix(t["summary"], 110),
                        "next_step": _prefix(t["next_step"], 110),
                    }
                    for t in tasks[:5]
                ]
                result["has_more"] = len(tasks) > 5
                return self._fit_list(
                    db,
                    context_id,
                    CONTEXT_BYTES,
                    result,
                    "tasks",
                    budget_bytes,
                    max(0, len(tasks) - 5),
                )
            return self._charge(db, context_id, CONTEXT_BYTES, result, budget_bytes)

    @staticmethod
    def _filters(since, until, record_type, status, detail, limit):
        since, until = _event_date(since), _event_date(until)
        if since is not None and until is not None and since > until:
            raise ValueError("since must not be after until.")
        if status not in ("active", "proposed", "superseded", "revoked", "all"):
            raise ValueError("Invalid lifecycle filter.")
        if detail not in ("summary", "full"):
            raise ValueError("detail must be summary or full.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 to 20.")
        clauses = []
        values = []
        for sql, val in (
            ("r.event_date>=?", since),
            ("r.event_date<=?", until),
            ("r.record_type=?", record_type),
        ):
            if val is not None:
                if not isinstance(val, str):
                    raise ValueError("Invalid query filter.")
                require_safe(val)
                clauses.append(sql)
                values.append(val)
        if status != "all":
            clauses.append("r.lifecycle=?")
            values.append(status)
        return clauses, values

    def search(
        self,
        context_id,
        query,
        *,
        actor="local",
        namespace=None,
        since=None,
        until=None,
        type=None,
        status="active",
        detail="summary",
        limit=6,
        budget_bytes=None,
        include_history=False,
    ):
        namespace = self._scope(namespace, actor)
        tokens = _terms(_bounded(query, "query", 300))
        require_safe(query)
        if not tokens or len(tokens) > 24:
            raise ValueError("Query must contain 1 to 24 terms. Use short keywords.")
        if not isinstance(include_history, bool):
            raise ValueError("include_history must be boolean.")
        self._filters(since, until, type, status, detail, limit)
        match = " AND ".join(f'"{token}"' for token in tokens)
        with self._transaction() as db:
            remaining, stale = self._context(db, context_id, namespace, actor)
            if stale:
                return {"status": "context_stale", "remaining_bytes": remaining}
            rows = db.execute(
                """SELECT s.*,r.record_type,r.event_date,r.imported_at,r.freshness,
              r.lifecycle,r.summary,r.summary_kind,r.summary_model,r.summary_prompt_sha256,
              r.summary_sha256,r.summary_generated_at,(s.sha256!=current.current_sha) AS historical
              FROM section_fts JOIN pm_sections s USING(section_id)
              JOIN pm_sources current ON current.namespace=s.namespace AND current.path=s.path
              JOIN pm_revisions r ON r.namespace=s.namespace AND r.path=s.path AND r.sha256=s.sha256
              WHERE s.namespace=? AND section_fts MATCH ?
                AND (? OR s.sha256=current.current_sha)
                AND (? IS NULL OR r.event_date>=?) AND (? IS NULL OR r.event_date<=?)
                AND (? IS NULL OR r.record_type=?) AND (?='all' OR r.lifecycle=?)
              ORDER BY bm25(section_fts),s.path,s.line_start,s.section_id LIMIT ?""",
                [
                    namespace,
                    match,
                    include_history,
                    since,
                    since,
                    until,
                    until,
                    type,
                    type,
                    status,
                    status,
                    limit + 1,
                ],
            ).fetchall()
            results = []
            for row in rows[:limit]:
                item = dict(row)
                item["historical"] = bool(item["historical"])
                item["preview"] = _prefix(item["text"], 220)
                if detail == "summary":
                    item = {
                        key: item[key]
                        for key in (
                            "section_id",
                            "path",
                            "sha256",
                            "heading",
                            "line_start",
                            "line_end",
                            "record_type",
                            "event_date",
                            "freshness",
                            "lifecycle",
                            "historical",
                            "preview",
                        )
                    } | {"summary": _prefix(row["summary"] or item["preview"], 120)}
                results.append(item)
            return self._fit_list(
                db,
                context_id,
                remaining,
                {"status": "ok", "results": results, "has_more": len(rows) > limit},
                "results",
                budget_bytes,
                max(0, len(rows) - limit),
            )

    def get(self, context_id, section_id, *, actor="local", namespace=None, budget_bytes=None):
        namespace = self._scope(namespace, actor)
        _bounded(section_id, "section_id", 64)
        with self._transaction() as db:
            remaining, stale = self._context(db, context_id, namespace, actor)
            if stale:
                return {"status": "context_stale", "remaining_bytes": remaining}
            row = db.execute(
                """SELECT s.*,(s.sha256!=current.current_sha) AS historical,
              r.event_date,r.freshness,r.lifecycle,r.summary,r.summary_kind,r.summary_model,
              r.summary_prompt_sha256,r.summary_sha256,r.summary_generated_at FROM pm_sections s
              JOIN pm_sources current ON current.namespace=s.namespace AND current.path=s.path
              JOIN pm_revisions r ON r.namespace=s.namespace AND r.path=s.path AND r.sha256=s.sha256
              WHERE s.namespace=? AND s.section_id=?""",
                (namespace, section_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown section_id.")
            section = dict(row)
            section["historical"] = bool(section["historical"])
            try:
                return self._charge(
                    db, context_id, remaining, {"status": "ok", "section": section}, budget_bytes
                )
            except BudgetTooSmall:
                if budget_bytes is None or remaining < budget_bytes:
                    return self._budget_error(remaining)
                section = {
                    key: section[key]
                    for key in (
                        "section_id",
                        "path",
                        "sha256",
                        "heading",
                        "line_start",
                        "line_end",
                        "historical",
                        "event_date",
                        "freshness",
                        "lifecycle",
                    )
                } | {"expand": True}
                return self._charge(
                    db, context_id, remaining, {"status": "ok", "section": section}, budget_bytes, 1
                )

    def checkpoint(
        self,
        task_id,
        summary,
        next_step,
        status,
        expected_revision,
        idempotency_key,
        *,
        actor="local",
        namespace=None,
    ):
        namespace = self._scope(namespace, actor)
        for value, field, maximum in (
            (task_id, "task_id", 96),
            (summary, "summary", 1200),
            (next_step, "next_step", 400),
            (idempotency_key, "idempotency_key", 96),
        ):
            _bounded(value, field, maximum)
            require_safe(value)
        if not isinstance(status, str) or status not in STATUS:
            raise ValueError("status must be open, blocked, or done.")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a nonnegative integer.")
        digest = hashlib.sha256(
            _json_bytes([task_id, summary, next_step, status, expected_revision])
        ).hexdigest()
        with self._transaction() as db:
            receipt = _read_receipt(db, namespace, actor, "checkpoint", idempotency_key, digest)
            if receipt is not None:
                return receipt
            storage_key = (
                idempotency_key
                if actor == "local"
                else hashlib.sha512(_json_bytes([actor, idempotency_key])).hexdigest()
            )
            old = db.execute(
                "SELECT revision,request_hash FROM pm_checkpoints "
                "WHERE namespace=? AND idempotency_key=?",
                (namespace, storage_key),
            ).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise ValueError("Idempotency key already has different checkpoint data.")
                return {"status": "saved", "task_id": task_id, "revision": old["revision"]}
            head = db.execute(
                "SELECT revision FROM pm_task_heads WHERE namespace=? AND task_id=?",
                (namespace, task_id),
            ).fetchone()
            current = head[0] if head else 0
            if current != expected_revision:
                raise ValueError(f"Checkpoint revision conflict. Current revision: {current}.")
            revision = current + 1
            operation_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO pm_checkpoints"
                "(namespace,task_id,revision,summary,next_step,status,idempotency_key,"
                "request_hash,actor,operation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    namespace,
                    task_id,
                    revision,
                    summary,
                    next_step,
                    status,
                    storage_key,
                    digest,
                    actor,
                    operation_id,
                ),
            )
            db.execute(
                "INSERT INTO pm_task_heads(namespace,task_id,revision) VALUES (?,?,?) "
                "ON CONFLICT(namespace,task_id) DO UPDATE SET revision=excluded.revision",
                (namespace, task_id, revision),
            )
            return _write_receipt(
                db,
                namespace,
                actor,
                "checkpoint",
                idempotency_key,
                digest,
                {
                    "status": "saved",
                    "namespace": namespace,
                    "task_id": task_id,
                    "revision": revision,
                    "operation_id": operation_id,
                },
            )

    def digest(self, context_id, since, until, *, actor="local", namespace=None, budget_bytes=None):
        namespace = self._scope(namespace, actor)
        self._filters(since, until, None, "active", "summary", 20)
        if since is None or until is None:
            raise ValueError("digest requires since and until dates.")
        with self._transaction() as db:
            remaining, stale = self._context(db, context_id, namespace, actor)
            if stale:
                return {"status": "context_stale", "remaining_bytes": remaining}
            rows = db.execute(
                """SELECT r.path,r.sha256,r.event_date,r.record_type,r.summary,
              r.summary_kind,r.summary_model,r.summary_prompt_sha256,r.freshness,r.lifecycle
              FROM pm_revisions r JOIN pm_sources s ON s.namespace=r.namespace
                AND s.path=r.path AND s.current_sha=r.sha256
              WHERE r.namespace=? AND r.event_date>=? AND r.event_date<=?
              ORDER BY r.event_date DESC,r.path LIMIT 21""",
                (namespace, since, until),
            ).fetchall()
            payload = {
                "status": "ok",
                "since": since,
                "until": until,
                "timezone": "Asia/Taipei",
                "items": [dict(row) for row in rows[:20]],
                "has_more": len(rows) > 20,
            }
            return self._fit_list(
                db, context_id, remaining, payload, "items", budget_bytes, max(0, len(rows) - 20)
            )
