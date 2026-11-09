# PM context pilot

This example exposes a project-scoped memory store through MCP stdio or authenticated Streamable HTTP. It is shared by MCP-capable agents and the model-free file uploader. Each service instance has a dedicated SQLite database and source root.

## Current capabilities

- Source SHA-256 revisions, heading/line-based section IDs, FTS5/BM25 lexical search with CJK bigrams, and explicit historical section reads.
- A task checkpoint with optimistic revisions and idempotent retries.
- A cumulative 8,000-byte UTF-8 application-payload budget per context. MCP envelopes, tool schemas, and model context replay have separate costs.
- HTTP reader/writer principals, actor-bound HTTP sessions, source compare-and-swap, and write receipts.

The pilot uses its own schema and entry points. The package-level commands continue to address the core memory engine; the new `mh pm` group calls this PM service. Batch 1 adds namespace/date filters, bounded summaries, a handoff template, additive lifecycle metadata, secret scanning and three-destination publication. Search remains FTS5/BM25 with CJK bigrams. Vector search and a complete decision replacement/confirmation workflow are outside this batch.

See [the batch-1 contract and acceptance record](BATCH1.md) before upgrading a database, and [cloud access options](CLOUD-ACCESS.md) before choosing an ingress path. This is development code with synthetic acceptance evidence, not a deployment instruction for an existing live service.

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

The server binds to `127.0.0.1`. A separately approved HTTPS proxy can provide private-network access. Each principals entry is keyed by actor and contains `role` and `token_sha256` (the SHA-256 digest of a high-entropy bearer token). Keep credential values and registry files outside source control. A file-backed registry is validated on every HTTP request; an invalid or unavailable file fails closed with 503. Removal of a credential rejects its next request, including requests on existing sessions. An in-memory registry supplied by an embedding test is static.

Every HTTP request supplies bearer authentication. HTTP sessions and contexts are actor-bound; contexts also bind namespace and source generation. Roles are `reader`, `agent_writer`, and `pm`; legacy `read`/`write` remain accepted. Reader tools include `boot`, `search`, `get`, and `source_status`. Writers additionally receive `ingest_text` and `checkpoint`. With `--extended-tools`, readers also receive digest/template/read/status tools and writers receive handoff write/retry. Agent writers can publish progress and proposals; active decision/requirement imports require the PM role. A separate human confirmer and decision-chain workflow remain batch 2. Unauthorized writes are rejected with JSON-RPC errors; inspect response bodies, not HTTP status alone.

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

`source_status` also returns `metadata_revision`. A same-text update to date, summary or other metadata requires the observed `expected_metadata_revision`. The source SHA alone cannot protect against concurrent metadata changes. Old clients sending no metadata retain existing metadata on a same-SHA no-op.

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

## Shared agent CLI

All vendors use the same plain-text CLI; model identity does not determine permissions. Inject `MEMORY_HALL_31_URL` and `MEMORY_HALL_31_TOKEN` through the approved credential mechanism. Never put token values in prompts, command arguments or repo files.

```sh
mh pm --namespace project:example boot EXAMPLE-1
mh pm --namespace project:example --budget-bytes 1024 search "release prerequisite"
mh pm --namespace project:example get SECTION_ID --context-id CONTEXT_ID
mh pm --namespace project:example digest --since 2026-11-01 --until 2026-11-09
mh pm --namespace project:example handoff template
mh pm --namespace project:example handoff write pm-line ./handoff.md \
  --expected-revision 0 --request-id example-handoff-1
mh pm --namespace project:example handoff status OPERATION_ID
mh pm --namespace project:example checkpoint EXAMPLE-1 \
  --summary "Synthetic review completed" --next-step "Check current CI" \
  --status open --expected-revision 0 --request-id example-checkpoint-1
```

Global PM options precede the subcommand. Search/get/digest/handoff-read create a context if none is supplied; pass the previous context ID to share its cumulative budget. Source or metadata changes require a new boot. Keep the same body, expected revision and request ID when retrying an uncertain write. Exit codes: 0 for a completed operation, 1 for an error, 2 for a pending/partial handoff. Output is JSON; do not treat an exit code alone as evidence of three-destination publication.

## Tests

Run from the repository using an environment with the declared development dependencies.

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s examples/pm_context -v
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider tests
```

The original 12 tests retain their assertions. Batch-1 tests add real SQLite migration, concurrent CAS, partial delivery recovery, safety/summary checks, plus a real loopback TCP server and a subprocess invoking `mh pm` through its Typer entry. In-process HTTP clients and a mock Ollama transport are used only in their explicitly scoped tests. Four actual vendor CLI sessions, TLS, cross-machine access, a real summary model, real source-line correctness and cloud main-thread receipts require separate acceptance receipts.

A complete core baseline collected and passed 148 tests on 2026-11-09 in 126.29 seconds. The slowest timeout-path fixture took 73.05 seconds. A 120-second whole-suite deadline is insufficient for that baseline. Preserve verbose output and choose a deadline that accommodates the timeout fixtures and host load.

Operational paths, real project documents and deployment credentials are managed separately. No live database, credentials, private ingress or cloud route is changed by these tests.
