# PM context pilot

This example exposes a project-scoped memory store through MCP stdio or authenticated Streamable HTTP. It is shared by MCP-capable agents and the model-free file uploader. Each service instance has a dedicated SQLite database and source root.

## Current capabilities

- Source SHA-256 revisions, heading/line-based section IDs, FTS5/BM25 lexical search with CJK bigrams, and explicit historical section reads.
- A task checkpoint with optimistic revisions and idempotent retries.
- A cumulative 8,000-byte UTF-8 application-payload budget per context. MCP envelopes, tool schemas, and model context replay have separate costs.
- HTTP reader/writer principals, actor-bound HTTP sessions, source compare-and-swap, and write receipts.

The pilot uses its own schema and entry points. The package-level `mh` CLI continues to address the core memory engine. The pilot currently has no namespace/date filters, vector search, handoff template, decision lifecycle, secret scanner, or three-destination publication.

## Local stdio

Use an isolated directory for approved text sources. All paths below are placeholders to replace with absolute paths on the execution host.

```sh
python examples/pm_context/server.py \
  --db /absolute/project/pilot.sqlite3 \
  --sources /absolute/project/sources
```

The local OS user is the stdio trust boundary. Treat source documents as evidence subject to the project permissions and approval process.

## Shared HTTP

```sh
python examples/pm_context/http_server.py \
  --db /absolute/project/pilot.sqlite3 \
  --sources /absolute/project/sources \
  --principals /absolute/private/principals.json \
  --port 8766
```

The server binds to `127.0.0.1`. A separately approved HTTPS proxy can provide private-network access. Each principals entry is keyed by actor and contains `role` (`read` or `write`) and `token_sha256` (the SHA-256 digest of a high-entropy bearer token). Keep credential values and registry files outside source control. The registry is loaded at startup.

Every HTTP request supplies bearer authentication. HTTP sessions are actor-bound. A reader sees `boot`, `search`, `get`, and `source_status`. A writer additionally sees `ingest_text` and `checkpoint`. Unauthorized write attempts are rejected at the MCP layer; clients must inspect the JSON-RPC/MCP response as well as HTTP status.

## Tool arguments

| Tool | Required arguments | Entry point |
| --- | --- | --- |
| `boot` | `task_id` | stdio and HTTP |
| `search` | `context_id`, `query` | stdio and HTTP |
| `get` | `context_id`, `section_id` | stdio and HTTP |
| `source_status` | `path` | HTTP |
| `checkpoint` | `task_id`, `summary`, `next_step`, `status`, `expected_revision`, `idempotency_key` | stdio and HTTP writer |
| `ingest_text` | `path`, `text`, `expected_sha256`, `idempotency_key` | HTTP writer |
| `ingest_source` | `path` | stdio |

Use `boot` once per task, then reuse its context ID for related searches and section reads. The checkpoint statuses are `open`, `blocked`, and `done`. Supply the observed revision when updating a checkpoint. Reuse the same idempotency key and exact payload when retrying an uncertain write.

A new source uses an empty `expected_sha256`. Before an update, call `source_status` and supply its current digest. A changed source invalidates existing contexts. Call `boot` again after `context_stale`.

HTTP imports preserve source text in SQLite. They do not materialize a separate source file. Keep approved originals in the project document system and retain their provenance.

## File upload

Inject `MEMORY_HALL_31_TOKEN` into the uploader environment through an approved credential mechanism.

```sh
python examples/pm_context/upload.py \
  https://approved-private-host.example/mcp \
  specs/example.md ./example.md \
  --request-id example-upload-1
```

For an existing source add `--expect <current-sha256>`. Keep the source bytes, expected version and request ID unchanged when retrying. Network failures produce explicit errors.

## Tests

Run from the repository using an environment with the declared development dependencies.

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s examples/pm_context -v
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider tests
```

The pilot suite has 12 synthetic tests using real SQLite, stdio subprocesses and an in-process HTTP client. The uploader test connects its transport to that client. Actual CLI, TCP, TLS and cross-machine acceptance require separate integration receipts.

A complete core baseline collected and passed 148 tests on 2026-11-09 in 126.29 seconds. The slowest timeout-path fixture took 73.05 seconds. A 120-second whole-suite deadline is insufficient for that baseline. Preserve verbose output and choose a deadline that accommodates the timeout fixtures and host load.

This baseline adds the existing six Python files without implementation changes. Operational paths, real project documents and deployment credentials are managed separately.
