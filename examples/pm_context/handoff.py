"""Durable handoff publication to three configured immutable local destinations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from budget import BudgetTooSmall
from handoff_template import FIRST_SECTION, SECTIONS, canonicalize, validate_handoff
from safety import require_safe

DESTINATIONS = ("archive", "pm_hub", "notes")
MAX_BODY = 2 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def verify_immutable(root: Path, filename: str, body: bytes, sha256: str) -> dict:
    """Read the configured target without following links or mutating its bytes."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("Destination is not a regular file.")
            received = file.read(MAX_BODY + 1)
        actual = hashlib.sha256(received).hexdigest()
        if received != body or actual != sha256:
            raise ValueError("Immutable destination content conflict.")
        return {
            "path": str(root / filename),
            "sha256": actual,
            "readback": True,
            "received_at": datetime.now(UTC).isoformat(),
        }
    finally:
        os.close(directory)


def write_immutable(root: Path, filename: str, body: bytes, sha256: str) -> dict:
    """Resume a matching staging prefix, then exclusively link the complete inode.

    Staging hardlinks remain as recovery evidence; no existing file is replaced.
    The directory and files reject symlinks, and the final artifact is read back.
    """
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        staged = f".{filename}.pending"
        fd = os.open(staged, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "r+b") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("Destination is not a regular file.")
            prefix = file.read(MAX_BODY + 1)
            if not body.startswith(prefix):
                raise ValueError("Immutable destination content conflict.")
            file.seek(len(prefix))
            file.write(body[len(prefix) :])
            file.flush()
            os.fsync(file.fileno())
            try:
                os.link(
                    staged,
                    filename,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            final_fd = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(final_fd, "rb") as artifact:
                if not stat.S_ISREG(os.fstat(artifact.fileno()).st_mode):
                    raise ValueError("Destination is not a regular file.")
                received = artifact.read(MAX_BODY + 1)
            actual_sha = hashlib.sha256(received).hexdigest()
            if received != body or actual_sha != sha256:
                raise ValueError("Immutable destination content conflict.")
            os.fsync(directory)
        return {
            "path": str(root / filename),
            "sha256": actual_sha,
            "readback": True,
            "received_at": datetime.now(UTC).isoformat(),
        }
    finally:
        os.close(directory)


class HandoffMixin:
    def _destinations(self):
        configured = self.handoff_destinations
        if not isinstance(configured, dict) or set(configured) != set(DESTINATIONS):
            raise ValueError("Configure exactly archive, pm_hub and notes handoff destinations.")
        roots = {name: Path(value).expanduser().absolute() for name, value in configured.items()}
        if len({root.resolve() for root in roots.values()}) != 3:
            raise ValueError("Handoff destinations must be three distinct directories.")
        return roots

    def handoff_write(
        self,
        line_id,
        body,
        expected_revision,
        idempotency_key,
        *,
        actor="local",
        namespace=None,
        event_date=None,
    ):
        from store import _bounded, _event_date

        namespace = self._scope(namespace, actor)
        self._destinations()
        _bounded(line_id, "line_id", 96)
        _bounded(idempotency_key, "idempotency_key", 96)
        require_safe(line_id)
        require_safe(idempotency_key)
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
            raise ValueError("expected_revision must be a nonnegative revision.")
        event_date = _event_date(event_date)
        # Scan before persisting, indexing, summarizing, or emitting any receipt.
        require_safe(body)
        canonical = canonicalize(body)
        validate_handoff(canonical)
        data = _bounded(canonical, "body", MAX_BODY).encode()
        digest = hashlib.sha256(data).hexdigest()
        request_hash = hashlib.sha256(
            _json([line_id, digest, expected_revision, event_date]).encode()
        ).hexdigest()
        operation_id = None
        with self._transaction() as db:
            replay = self._receipt_read(
                db, namespace, actor, "handoff_write", idempotency_key, request_hash
            )
            if replay is not None:
                operation_id = replay["operation_id"]
            else:
                head = db.execute(
                    "SELECT accepted_revision,published_revision FROM pm_handoff_heads "
                    "WHERE namespace=? AND line_id=?",
                    (namespace, line_id),
                ).fetchone()
                current = head["accepted_revision"] if head else 0
                published = head["published_revision"] if head else 0
                if current != expected_revision:
                    raise ValueError(f"Handoff revision conflict. Current revision: {current}.")
                if current != published:
                    pending = db.execute(
                        "SELECT operation_id FROM pm_handoff_commits "
                        "WHERE namespace=? AND line_id=? AND revision=?",
                        (namespace, line_id, current),
                    ).fetchone()
                    raise ValueError(f"Handoff publication pending. Retry operation {pending[0]}.")
                operation_id = uuid.uuid4().hex
                revision = current + 1
                source_path = "_handoffs/" + hashlib.sha256(line_id.encode()).hexdigest() + ".md"
                source = self._current_source(db, namespace, source_path)
                expected_sha = source["current_sha"] if source else ""
                db.execute(
                    """INSERT INTO pm_handoff_commits
                  (operation_id,namespace,line_id,revision,actor,request_hash,body,sha256,event_date,
                   source_path,expected_source_sha,state,created_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                    (
                        operation_id,
                        namespace,
                        line_id,
                        revision,
                        actor,
                        request_hash,
                        data,
                        digest,
                        event_date,
                        source_path,
                        expected_sha,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                db.executemany(
                    "INSERT INTO pm_handoff_deliveries(operation_id,destination_id,state) "
                    "VALUES (?,?,'pending')",
                    ((operation_id, name) for name in DESTINATIONS),
                )
                db.execute(
                    "INSERT INTO pm_handoff_heads"
                    "(namespace,line_id,accepted_revision,published_revision) "
                    "VALUES (?,?,?,?) ON CONFLICT(namespace,line_id) DO UPDATE SET "
                    "accepted_revision=excluded.accepted_revision",
                    (namespace, line_id, revision, published),
                )
                self._receipt_write(
                    db,
                    namespace,
                    actor,
                    "handoff_write",
                    idempotency_key,
                    request_hash,
                    {
                        "operation_id": operation_id,
                        "state": "pending",
                        "revision": revision,
                        "namespace": namespace,
                        "sha256": digest,
                    },
                )
        # A replay does not redo completed targets; current progress is explicit.
        return self.handoff_retry(operation_id, actor=actor, namespace=namespace)

    def _handoff_row(self, db, operation_id, namespace):
        from store import _bounded

        _bounded(operation_id, "operation_id", 32)
        row = db.execute(
            "SELECT * FROM pm_handoff_commits WHERE namespace=? AND operation_id=?",
            (namespace, operation_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown handoff operation.")
        return row

    def handoff_status(self, operation_id, *, actor="local", namespace=None):
        namespace = self._scope(namespace, actor)
        with closing(self._connect()) as db:
            row = self._handoff_row(db, operation_id, namespace)
            deliveries = db.execute(
                "SELECT destination_id,state,actual_sha,receipt,attempts,error_code "
                "FROM pm_handoff_deliveries WHERE operation_id=? ORDER BY destination_id",
                (operation_id,),
            ).fetchall()
            blob = db.execute(
                "SELECT content FROM pm_source_texts WHERE namespace=? AND path=? AND sha256=?",
                (namespace, row["source_path"], row["sha256"]),
            ).fetchone()
        return {
            "operation_id": operation_id,
            "namespace": namespace,
            "line_id": row["line_id"],
            "revision": row["revision"],
            "actor": row["actor"],
            "sha256": row["sha256"],
            "state": row["state"],
            "source_path": row["source_path"],
            "database_receipt": {"sha256": hashlib.sha256(blob[0]).hexdigest(), "readback": True}
            if blob
            else None,
            "destinations": [
                {
                    **{
                        k: r[k]
                        for k in ("destination_id", "state", "actual_sha", "attempts", "error_code")
                    },
                    "receipt": json.loads(r["receipt"]) if r["receipt"] else None,
                }
                for r in deliveries
            ],
        }

    def handoff_retry(self, operation_id, *, actor="local", namespace=None):
        namespace = self._scope(namespace, actor)
        roots = self._destinations()
        with closing(self._connect()) as db:
            row = dict(self._handoff_row(db, operation_id, namespace))
        if row["state"] == "published":
            return self.handoff_status(operation_id, actor=actor, namespace=namespace)
        filename = f"{operation_id}-{row['sha256']}.md"
        for name in DESTINATIONS:
            with closing(self._connect()) as db:
                state = db.execute(
                    "SELECT state FROM pm_handoff_deliveries "
                    "WHERE operation_id=? AND destination_id=?",
                    (operation_id, name),
                ).fetchone()[0]
            if state == "completed":
                continue
            try:
                receipt = write_immutable(roots[name], filename, row["body"], row["sha256"])
            except (OSError, ValueError) as exc:
                code = (
                    "content_conflict" if isinstance(exc, ValueError) else "destination_unavailable"
                )
                with self._transaction() as db:
                    db.execute(
                        "UPDATE pm_handoff_deliveries "
                        "SET state='failed',attempts=attempts+1,error_code=? "
                        "WHERE operation_id=? AND destination_id=? AND state!='completed'",
                        (code, operation_id, name),
                    )
            else:
                with self._transaction() as db:
                    db.execute(
                        "UPDATE pm_handoff_deliveries SET state='completed',actual_sha=?,receipt=?,"
                        "attempts=attempts+1,error_code=NULL "
                        "WHERE operation_id=? AND destination_id=?",
                        (receipt["sha256"], _json(receipt), operation_id, name),
                    )
        # A previously completed target may have changed while another was offline.
        # Re-read every actual artifact before the final publication transaction.
        for name in DESTINATIONS:
            try:
                verified = verify_immutable(roots[name], filename, row["body"], row["sha256"])
            except (OSError, ValueError) as exc:
                code = (
                    "content_conflict" if isinstance(exc, ValueError) else "destination_unavailable"
                )
                with self._transaction() as db:
                    db.execute(
                        "UPDATE pm_handoff_deliveries SET state='failed',error_code=? "
                        "WHERE operation_id=? AND destination_id=?",
                        (code, operation_id, name),
                    )
            else:
                with self._transaction() as db:
                    db.execute(
                        "UPDATE pm_handoff_deliveries SET receipt=?,actual_sha=? "
                        "WHERE operation_id=? AND destination_id=? AND state='completed'",
                        (_json(verified), verified["sha256"], operation_id, name),
                    )
        with self._transaction() as db:
            current = self._handoff_row(db, operation_id, namespace)
            completed = db.execute(
                "SELECT count(*) FROM pm_handoff_deliveries WHERE operation_id=? "
                "AND state='completed' AND actual_sha=?",
                (operation_id, row["sha256"]),
            ).fetchone()[0]
            if completed == 3 and current["state"] != "published":
                source = self._current_source(db, namespace, row["source_path"])
                actual = source["current_sha"] if source else ""
                if actual != row["expected_source_sha"]:
                    raise ValueError("Handoff source CAS conflict. Publication remains pending.")
                metadata = self._metadata(
                    row["body"], record_type="handoff", event_date=row["event_date"]
                )
                self._index_source(
                    db,
                    namespace,
                    row["source_path"],
                    row["body"],
                    row["sha256"],
                    metadata,
                    expected_metadata_revision=source["metadata_revision"] if source else None,
                )
                db.execute(
                    "UPDATE pm_handoff_commits SET state='published' WHERE operation_id=?",
                    (operation_id,),
                )
                db.execute(
                    "UPDATE pm_handoff_heads SET published_revision=? "
                    "WHERE namespace=? AND line_id=? "
                    "AND accepted_revision=?",
                    (row["revision"], namespace, row["line_id"], row["revision"]),
                )
        return self.handoff_status(operation_id, actor=actor, namespace=namespace)

    def handoff_read(
        self, context_id, line_id, *, actor="local", namespace=None, budget_bytes=4096
    ):
        from store import _bounded

        namespace = self._scope(namespace, actor)
        if budget_bytes is None:
            budget_bytes = 4096
        _bounded(line_id, "line_id", 96)
        require_safe(line_id)
        with self._transaction() as db:
            remaining, stale = self._context(db, context_id, namespace, actor)
            if stale:
                return {"status": "context_stale", "remaining_bytes": remaining}
            row = db.execute(
                """SELECT c.* FROM pm_handoff_heads h JOIN pm_handoff_commits c
              ON c.namespace=h.namespace AND c.line_id=h.line_id AND c.revision=h.published_revision
              WHERE h.namespace=? AND h.line_id=? AND c.state='published'""",
                (namespace, line_id),
            ).fetchone()
            if row is None:
                return self._charge(
                    db,
                    context_id,
                    remaining,
                    {"status": "not_found", "line_id": line_id},
                    budget_bytes,
                )
            body = row["body"].decode()
            payload = {
                "status": "ok",
                "state": "published",
                "line_id": line_id,
                "revision": row["revision"],
                "operation_id": row["operation_id"],
                "sha256": row["sha256"],
                "body": body,
                "source_path": row["source_path"],
            }
            try:
                return self._charge(db, context_id, remaining, payload, budget_bytes)
            except BudgetTooSmall:
                chapters = validate_handoff(body)
                payload["body"] = f"## {FIRST_SECTION}\n{chapters[FIRST_SECTION]}\n"
                payload["omitted_chapters"] = [SECTIONS[i] for i in (1, 2, 5, 6, 7)]
                payload["expand"] = {"path": row["source_path"], "sha256": row["sha256"]}
                from store import _prefix

                for preview_bytes in (400, 200, 80):
                    payload["chapter_previews"] = [
                        {
                            "chapter": SECTIONS[i],
                            "text": _prefix(chapters[SECTIONS[i]], preview_bytes),
                            "truncated": len(chapters[SECTIONS[i]].encode()) > preview_bytes,
                        }
                        for i in (3, 4)
                    ]
                    try:
                        return self._charge(
                            db, context_id, remaining, payload, budget_bytes, len(SECTIONS) - 1
                        )
                    except BudgetTooSmall:
                        continue
                if remaining < budget_bytes:
                    return self._budget_error(remaining)
                raise BudgetTooSmall(
                    "The handoff first three actions and pointers exceed the budget."
                ) from None
