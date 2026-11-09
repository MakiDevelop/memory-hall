"""Additive, transactional v1 -> v2 migration for synthetic/local PM stores."""

V1_APPLICATION_ID = 1296577329
V2_APPLICATION_ID = 1296577330

SCHEMA = """
CREATE TABLE pm_namespaces (namespace TEXT PRIMARY KEY, source_generation INTEGER NOT NULL);
CREATE TABLE pm_sources (
 namespace TEXT NOT NULL, path TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE,
 current_sha TEXT NOT NULL, PRIMARY KEY(namespace,path));
CREATE TABLE pm_revisions (
 namespace TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 section_count INTEGER NOT NULL, record_type TEXT NOT NULL, event_date TEXT,
 imported_at TEXT, freshness TEXT NOT NULL, lifecycle TEXT NOT NULL,
 summary TEXT, summary_kind TEXT, summary_model TEXT, summary_prompt_sha256 TEXT,
 summary_sha256 TEXT, summary_generated_at TEXT, verified_at TEXT, review_after TEXT,
 supersedes TEXT, metadata_revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(namespace,path,sha256));
CREATE TABLE pm_sections (
 section_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT NOT NULL, heading TEXT NOT NULL, line_start INTEGER NOT NULL,
 line_end INTEGER NOT NULL, text TEXT NOT NULL);
CREATE INDEX pm_sections_source ON pm_sections(namespace,path,sha256);
CREATE TABLE pm_contexts (
 context_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, actor TEXT,
 source_generation INTEGER NOT NULL, remaining_bytes INTEGER NOT NULL CHECK(remaining_bytes>=0));
CREATE TABLE pm_checkpoints (
 namespace TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 summary TEXT NOT NULL, next_step TEXT NOT NULL, status TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, actor TEXT,
 operation_id TEXT, PRIMARY KEY(namespace,task_id,revision), UNIQUE(namespace,idempotency_key));
CREATE TABLE pm_task_heads (
 namespace TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(namespace,task_id));
CREATE TABLE pm_source_texts (
 namespace TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 content BLOB NOT NULL, PRIMARY KEY(namespace,path,sha256));
CREATE TABLE pm_write_receipts (
 namespace TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, receipt TEXT NOT NULL,
 PRIMARY KEY(namespace,actor,operation,idempotency_key));
CREATE TABLE pm_handoff_commits (
 operation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, line_id TEXT NOT NULL,
 revision INTEGER NOT NULL, actor TEXT NOT NULL, request_hash TEXT NOT NULL,
 body BLOB NOT NULL, sha256 TEXT NOT NULL, event_date TEXT, source_path TEXT NOT NULL,
 expected_source_sha TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(namespace,line_id,revision));
CREATE TABLE pm_handoff_heads (
 namespace TEXT NOT NULL, line_id TEXT NOT NULL, accepted_revision INTEGER NOT NULL,
 published_revision INTEGER NOT NULL, PRIMARY KEY(namespace,line_id));
CREATE TABLE pm_handoff_deliveries (
 operation_id TEXT NOT NULL, destination_id TEXT NOT NULL, state TEXT NOT NULL,
 actual_sha TEXT, receipt TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, PRIMARY KEY(operation_id,destination_id));
"""


def migrate(db, default_namespace: str) -> None:
    """Caller holds BEGIN IMMEDIATE and has validated application/root ownership."""
    version = db.execute("SELECT value FROM store_config WHERE key='pm_schema_version'").fetchone()
    if version:
        if version[0] != "2":
            raise ValueError("Unsupported PM schema version.")
        configured = db.execute(
            "SELECT value FROM store_config WHERE key='default_namespace'"
        ).fetchone()
        if configured is None or configured[0] != default_namespace:
            raise ValueError("This database belongs to a different default namespace.")
        columns = {row[1] for row in db.execute("PRAGMA table_info(pm_revisions)")}
        if "metadata_revision" not in columns:
            db.execute(
                "ALTER TABLE pm_revisions ADD COLUMN metadata_revision INTEGER NOT NULL DEFAULT 1"
            )
        return
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    generation = db.execute(
        "SELECT value FROM store_config WHERE key='source_generation'"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO pm_namespaces(namespace,source_generation) VALUES (?,?)",
        (default_namespace, int(generation)),
    )
    copies = (
        ("sources", "pm_sources", "path,source_id,current_sha"),
        ("sections", "pm_sections", "section_id,path,sha256,heading,line_start,line_end,text"),
        ("task_heads", "pm_task_heads", "task_id,revision"),
        ("source_texts", "pm_source_texts", "path,sha256,content"),
        (
            "remote_write_receipts",
            "pm_write_receipts",
            "actor,operation,idempotency_key,request_hash,receipt",
        ),
    )
    # These identifiers are fixed code constants, never caller-controlled SQL.
    for old, new, columns in copies:
        db.execute(
            f"INSERT INTO {new}(namespace,{columns}) SELECT ?,{columns} FROM {old}",  # noqa: S608
            (default_namespace,),
        )
        if (
            db.execute(f"SELECT count(*) FROM {old}").fetchone()[0]  # noqa: S608
            != db.execute(f"SELECT count(*) FROM {new}").fetchone()[0]  # noqa: S608
        ):
            raise ValueError("Migration row verification failed.")
    db.execute(
        """INSERT INTO pm_revisions
      (namespace,path,sha256,section_count,record_type,freshness,lifecycle)
      SELECT ?,path,sha256,section_count,'unknown','unverified','active' FROM revisions""",
        (default_namespace,),
    )
    db.execute(
        """INSERT INTO pm_contexts
      (context_id,namespace,actor,source_generation,remaining_bytes)
      SELECT context_id,?,NULL,source_generation,remaining_bytes FROM contexts""",
        (default_namespace,),
    )
    db.execute(
        """INSERT INTO pm_checkpoints
      (namespace,task_id,revision,summary,next_step,status,idempotency_key,request_hash)
      SELECT ?,task_id,revision,summary,next_step,status,idempotency_key,request_hash
      FROM checkpoints""",
        (default_namespace,),
    )
    checks = copies + (
        ("revisions", "pm_revisions", "path,sha256,section_count"),
        ("contexts", "pm_contexts", "context_id,source_generation,remaining_bytes"),
        (
            "checkpoints",
            "pm_checkpoints",
            "task_id,revision,summary,next_step,status,idempotency_key,request_hash",
        ),
    )
    for old, new, columns in checks:
        # Fixed identifiers only; namespace remains a bound value.
        if (
            db.execute(  # noqa: S608
                f"SELECT {columns} FROM {old} EXCEPT SELECT {columns} FROM {new} WHERE namespace=?",  # noqa: S608
                (default_namespace,),
            ).fetchone()
            is not None
        ):
            raise ValueError("Migration value verification failed.")
    db.execute(
        "INSERT INTO store_config(key,value) VALUES ('default_namespace',?)", (default_namespace,)
    )
    db.execute("INSERT INTO store_config(key,value) VALUES ('pm_schema_version','2')")
    # The previous binary refuses this ID, preventing writes into frozen v1 tables.
    db.execute("PRAGMA application_id = 1296577330")
