# Batch 1: PM context contract and acceptance

This document describes the development implementation. Public examples use synthetic data and placeholder paths. It does not authorize live migration, real document ingestion, network changes or credential issuance.

## Why a PM uses it

Keep original documents and their source-line mapping as evidence. Store short progress/checkpoint records and retrieve only the portions needed for the current decision. Start with the latest checkpoint or completely published handoff; use relevance search for evidence rather than assuming the first search result is the current project state. A summary is an index aid, not permission to deploy, merge or activate a feature.

Agents should:

1. Boot the current task; retain its revision, context ID and remaining budget.
2. Search with a narrow query and summary detail. Get the anchored section only when needed; cite its original path/line references separately from the excerpt's local lines.
3. Explicitly report stale, historical, proposed and unverified material. If the answer is missing, report the gap instead of treating absence as proof.
4. Write progress with observed revisions and stable idempotency keys. Submit a decision as proposed unless the authenticated principal has separately authorized publishing capability.
5. Publish a fixed eight-section handoff and inspect all destination receipts. Resume a partial operation by ID; do not create another revision to hide a failed delivery.

## Isolated server configuration

The sources directory and three destination directories must already exist and be approved by their owner. The three destinations must be distinct absolute paths, configured by the service administrator, never by a tool caller.

```sh
python examples/pm_context/http_server.py \
  --db /absolute/sandbox/pilot.sqlite3 --sources /absolute/sandbox/sources \
  --principals /absolute/private/principals.json --namespace project:example \
  --extended-tools \
  --handoff-archive /absolute/sandbox/archive \
  --handoff-pm-hub /absolute/sandbox/pm-hub \
  --handoff-notes /absolute/sandbox/notes/handoff
```

Optional model summaries require both `--summary-url` and `--summary-model`. The adapter calls configured Ollama `/api/generate` with a versioned system prompt, `stream=false`, temperature 0, a 256-token output cap and a 10-second timeout. Input is at most 64 KiB and accepted summary output at most 300 Unicode codepoints. Model failure, incomplete/oversized/different-model output and sensitive output fail explicitly. There is no silent model-to-extractive fallback. Real model quality, runtime capacity and cost remain unverified. See [Ollama's API contract](https://docs.ollama.com/api/generate).

The default tool catalog retains legacy names. `--extended-tools` enables digest and handoff tools. New fields on existing tools are optional; unknown fields remain rejected. `mh pm --help` documents the CLI without changing the existing core commands.

## Principals and error contract

A registry entry has a role, token digest, an explicit `namespaces` array and `default_namespace` within that array. Legacy entries without a scope are limited to the configured default namespace. There is no wildcard scope. Example shape (replace the placeholder outside source control):

```json
{
  "example-progress-runner": {
    "role": "agent_writer",
    "token_sha256": "<64 hexadecimal characters from an approved random bearer token>",
    "namespaces": ["project:example"],
    "default_namespace": "project:example"
  }
}
```

Every tool data call receives the verified actor from HTTP middleware; clients cannot set actor. Both context ownership and namespace capability are checked, including context reads with no namespace argument. Registry changes are checked again after acquiring a session lock. Revoked credentials return HTTP 401; unavailable/invalid registry returns 503; changed role/scope/digest invalidates an old session with 404 and requires initialization again. This is next-request invalidation, not cancellation of work already running.

Unknown or unauthorized tools return a JSON-RPC error even when the HTTP transport is 200. Validation failures return MCP `isError`; neither is a successful write. Tests assert refusal **and unchanged business state**. `agent_writer` can write checkpoint/handoff and proposed decision/requirement sources. `pm` can import active ones. The legacy `write` role retains progress-writing capability; publishing active decisions requires an explicitly configured `pm` role. This code does not create or authorize any live principal.

## Data and version semantics

| Concept | Representation |
| --- | --- |
| Task state | `open`, `blocked`, `done` |
| Decision lifecycle foundation | `proposed`, `active`, `superseded`, `revoked` metadata |
| Source check | `verified`, `stale`, `unverified` |
| Document version | Computed `historical` flag relative to current source SHA |
| Event date | Explicit `YYYY-MM-DD` or NULL; never inferred from import time |
| Import time | Separate UTC timestamp for new imports; legacy unknown retained NULL |
| Summary provenance | Kind, source SHA, generated time, summary SHA; model/prompt SHA when model-generated |

Date filters are inclusive project calendar dates in Asia/Taipei: since at local midnight through the instant before the day after until. Stored event dates have day precision, so filtering compares calendar dates directly; this does not invent an event instant. Date-filtered results exclude unknown dates. `digest` requires both bounds. Search defaults to current, active records; proposals require `status=proposed`. Historical versions require explicit opt-in. Old section IDs remain retrievable as evidence with historical status.

`digest` includes all four lifecycle states for current source metadata in the requested date range. It is a dated snapshot, not a historical change ledger; source content changes are not inferred decision reversals. The complete replacement chain and its change events remain batch 2.

`source_status` returns source SHA plus metadata revision. New content uses source-SHA CAS; changing metadata without changing text also requires metadata-revision CAS. Updates invalidate only that namespace's contexts. Source SHA changes do not automatically supersede a decision. Full bidirectional replacement chains, conflict resolution and human confirmation are batch 2; the fields alone are not an enforcement workflow.

## Bytes, truncation and summaries

Successful context reads (`boot`, `search`, `get`, `digest`, `handoff_read`) include `used_bytes`, `budget`, `remaining_context_bytes`, `truncated_count` and conservative `charged_bytes`. `remaining_bytes` is a legacy alias. `used_bytes` is the final compact JSON business payload encoded as UTF-8, including these fields. A call is bounded by its configured ceiling (1–8,000 bytes) and the context's remaining cumulative 8,000 bytes. `handoff_read` defaults to 4,096 bytes. Charges may exceed the final serialized size slightly to resolve numeric-width changes conservatively, never undercharge it.

When content does not fit, return a bounded selection or expandable pointer with truncation metadata. Tiny budgets that cannot fit minimum metadata return `budget_too_small`; exhausted contexts return the legacy explicit exhausted status. Error/status envelopes are bounded but cannot obey a one-byte requested content ceiling and are not charged to the content budget. Validation messages are limited to 384 UTF-8 bytes; request IDs to 256 bytes. No claim is made that the 8 KB budget bounds MCP schemas, outer envelopes, HTTP bytes, repeated model context, user prompts or the agent's final answer. These require separate provider/wire measurements. The CLI pretty-prints JSON for humans; its stdout byte count therefore differs from compact business-payload accounting.

Summary search returns small pointers and the source summary, rather than duplicating full provenance on every hit. Full detail/get supplies provenance, including summary kind. Extractive summaries are marked `[extractive]`; stored manual summaries are explicitly marked manual. All are at most 300 codepoints.

Model ingestion checks a receipt keyed by the raw request before generating a summary. Retries can return the original result with an offline, changed or absent summarizer; changing the source request or switching summary mode with the same key is rejected. A deliberately new summary needs a new key and applicable source/metadata CAS. To serialize equal requests, the bounded model call currently holds a SQLite write transaction; concurrent writes can wait for the configured model timeout. This prototype tradeoff needs latency/load measurement before broader use.

In the 20-query synthetic paired test, exact result IDs match with no truncation:

| Measure | Full | Summary | Reduction |
| --- | ---: | ---: | ---: |
| Business payload including metadata | 68,340 bytes | 17,340 bytes | 74.63% |
| Text/summary content only | 30,190 bytes | 6,760 bytes | 77.61% |

This fixture demonstrates response size on repetitive synthetic documents. It does not measure real PM answer correctness or total token savings. A fixed-model, fixed-data provider-usage comparison remains required before claiming a project-level improvement.

## Handoff publication

`handoff_template` provides eight fixed chapters. Chapter 1 requires exactly three nonempty single-line actions and is limited to 1,536 UTF-8 bytes. The unverified chapter can be empty. Large reads preserve chapter 1 completely, plus bounded previews and pointers for work-in-progress and pending decisions.

Canonical handoff bytes are UTF-8: remove the initial BOM, normalize CR/CRLF to LF and retain one terminal newline. Preserve Unicode codepoints, leading blank lines and trailing spaces within lines. Normal document uploads preserve their original bytes instead of applying handoff canonicalization.

A commit persists body, operation ID, expected revision, hash, actor and outbox deliveries in SQLite. Three immutable files are then delivered to archive, PM handoff directory and local notes directory. Each is read back and hashed; publication rechecks all three before atomically advancing the published head and indexing the body. Partial writes stay visible in status but are absent from handoff reads/search. Another revision cannot jump past a pending commit. Retry uses the same operation/body and never overwrites a conflicting immutable destination. Completed artifacts modified during a partial delivery block publication. Staging files are retained for recovery; retention cleanup requires a separate approved policy.

The archive receipt includes a file readback in addition to the SQLite BLOB. All three SHA values must match the canonical body SHA. A local notes receipt proves the local file only. NAS/Git council synchronization is not evidence of transfer to a cloud project folder. Cloud delivery and a main-thread acknowledgment need independent receipts.

## Safety and migration boundary

Raw content and metadata strings are scanned before indexing, model summarization or replication. Findings expose category and line only. Fixtures cover API-key formats, password assignments, email, Taiwan phone/ID and quotation text, with SHA false-positive cases. This finite scanner cannot guarantee detection of every secret or PII. No self-service bypass exists. The separately approved SHA/operation-bound exception workflow is not implemented in batch 1; rejected material must be redacted or await that workflow.

Opening a legacy database with the new Store performs a transactional additive migration to `pm_*` tables and validates preserved values. Legacy tables remain. Original source/section IDs, SHA, checkpoint revisions and receipts are retained in the configured default namespace. Old unbound contexts cannot become remote authenticated contexts. Database application ID changes from 1296577329 to 1296577330, so an old binary refuses the migrated file rather than writing frozen legacy tables. Default namespace and source root remain bound to the database.

**Do not point development code at a live database.** Before a separately authorized upgrade, stop conflicting writers or take a SQLite-consistent backup including the relevant file/archive state, rehearse on an isolated copy with the same source-root mapping, compare source hashes/checkpoint revisions and test rollback. Rollback uses the pre-upgrade copy with the old binary; it is not an in-place downgrade. Backup schedules and real restore drills are not delivered by this batch.

## Acceptance scope

Local evidence: original 12 assertions retained; 62 total PM tests passed, including 11 budget/handler/TCP/CLI checks; core 148 passed in 126.44 seconds; Ruff passed for `src`, `tests` and the PM example. Real SQLite is used for data tests. HTTP unit tests and Ollama MockTransport tests make no TLS/model claims. The existing CI runs the core suite and Docker smoke; the example PM suite was run separately without changing CI configuration.

Outstanding acceptance: four actual vendor CLI sessions with their own identities and cross-reads; real source-line correctness including the approved K234 corpus; real model summaries; private HTTPS and cross-machine regression; fixed-model provider-token comparison; cloud parent/main-thread delivery. Synthetic actor names and the common `mh pm` executable are not substitutes for those receipts. No real documents, live credentials, live database or network configuration were changed.
