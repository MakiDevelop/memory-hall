# memory-hall — Design

> Status: **Accepted** v0.1 (2026-04-18) — incorporates Codex dissent + Gemini architecture review + Max best practice + Grok community pulse

## 1. Purpose

A self-hostable memory layer designed for people running **multiple AI agents** (Claude, Codex, Gemini, local LLMs, …) and wanting them to share state across sessions and tools.

Existing tools are wrong-shaped for this:

| Tool | Why it doesn't fit |
|---|---|
| **mem0 OSS** | Sync LLM call on every write → P99 unreliable past ~30s payloads. Fact extraction quality issues on long-running stores ([mem0ai/mem0#4573](https://github.com/mem0ai/mem0/issues/4573)). Claude-only MCP path. |
| **Letta** | Full agent runtime, not a memory layer — too opinionated. Memory primitives worth borrowing (parallel agent isolation in v0.23.x). |
| **Zep / Graphiti** | Requires Neo4j or FalkorDB — heavier than personal infra should bear. |
| **Mnemosyne** | Closest match, but TypeScript and lacks multi-agent identity layer. |

Independent precedent (community, April 2026): the [ERINYS project](https://x.com/GhostyAIpp/status/2044815702339813552) used SQLite + FTS5 + sqlite-vec to reach LongMemEval-S 100% / LoCoMo 94% with zero LLM calls in the write path. That pattern is the architectural anchor for memory-hall.

## 2. Goals

- **G1** — Local-only write P99 < 100ms (zero LLM call on write path; remote-embedder deployments have softer targets, see §8)
- **G2** — Cross-agent (Claude / Codex / Gemini / Max / Grok / local LLMs / humans)
- **G3** — Cross-session (open-ended pickup, not chat-bound)
- **G4** — Search quality matching or exceeding mem0 for "find what I wrote about X"
- **G5** — Multi-tenant data model from day one (`tenant_id` first-class); v0.1 runtime is single-tenant `default` (see ADR 0002)
- **G6** — Min local (Ollama + sqlite-vec, zero external deps), max distributed (Qdrant + remote embedder host, multi-machine)
- **G7** — No middleware monopoly: HTTP REST is primary, MCP and CLI are convenience layers, none is the only legal path

## 3. Non-Goals

- **N1** — Sync fact extraction on write (optional async enrichment in v2)
- **N2** — Migrating existing mem0 stores in v0.1 (read-only legacy access; `mh import-mem0` script committed for v0.2 — see ADR 0001)
- **N3** — UI / TUI / graph visualization (future, separate package)
- **N4** — SaaS hosting (self-host first; SaaS only if community demand)
- **N5** — Replacing chat-bound memory (this is for agents, not consumer chat apps)

## 4. Scope (memory-hall vs memory-gateway)

memory-hall is the **engine library**:
- Storage, embedder, search, CLI, optional minimal FastAPI server for solo use
- Pip-installable, runs without auth in local-only setups

[`memory-gateway`](https://github.com/USERNAME/memory-gateway) is the **deployment platform** that wraps memory-hall with HMAC auth, ACL, staging, proposals, and event-sourced governance.

Solo users: `memory-hall serve` is enough. Production / multi-user: deploy `memory-gateway`, which embeds memory-hall as a library.

See [ADR 0003](adr/0003-engine-library-vs-deployment-platform.md) for the full split rationale.

## 5. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  AGENTS (any combination)                                     │
│  Claude / Codex / Gemini / local LLM / humans / bots          │
└────┬───────────────┬──────────────┬───────────────────────────┘
     │ MCP (v0.2+)   │ CLI (mh)     │ HTTP REST or UDS
     ↓               ↓              ↓
┌──────────────────────────────────────────────────────────────┐
│  memory-hall.engine                                           │
│  - storage / embedder / search                                │
│  - optional standalone FastAPI server (no auth, local-only)   │
│  - middleware: tenant + dev-mode auth (full HMAC in gateway)  │
└────┬─────────────────────────────────────┬───────────────────┘
     │ write                               │ search
     ↓                                     ↓
┌──────────────────────┐    ┌─────────────────────────────────┐
│  Storage             │    │  Vector store                    │
│  - SQLite (WAL)      │    │  - sqlite-vec v0.1.6 (default)   │
│  - aiosqlite client  │    │  - qdrant adapter (when scale    │
│  - single-writer Q   │    │     >50K vectors or multi-host)  │
└──────────────────────┘    └─────────────────────────────────┘
                                          ↑
                                          │ embed-on-write (2s timeout)
                                          │
                       ┌──────────────────────────────┐
                       │  Embedder (Protocol)          │
                       │  - ollama bge-m3 (default)    │
                       │  - openai                     │
                       │  - sentence-transformers      │
                       └──────────────────────────────┘
```

### Three entry points

1. **HTTP REST** — primary, language-agnostic
2. **MCP server** (v0.2+) — wraps HTTP for Claude convenience
3. **CLI `mh`** — wraps HTTP for Codex / shell / non-MCP agents

Optional **Unix Domain Socket** transport for same-host agents (Codex CLI, local scripts) avoids TCP overhead.

All paths hit the same engine. **No path is privileged** — any one being down doesn't break the others.

## 6. Data Model

### Entry (v0.1 schema)
```python
{
    "entry_id": "01HX...",        # ULID, sortable, primary key
    "tenant_id": "default",       # multi-tenant partition (v0.1 always "default")
    "agent_id": "claude",         # who wrote this
    "namespace": "home",          # logical category (home/work/project:X/agent:X/shared)
    "type": "decision",           # episode/decision/observation/experiment/fact/note/question/answer
    "content": "...",             # raw text, recommended <8KB
    "content_hash": "sha256:...", # UNIQUE (per tenant) — insert-level dedup, prevents wrap-up duplicate noise
    "summary": null,              # optional one-line summary
    "tags": ["mem0"],
    "references": ["01HX..."],    # graph edges to other entries
    "metadata": {},               # free-form
    "embedding": null,            # server-computed
    "sync_status": "embedded",    # PENDING / EMBEDDED / FAILED — write-path state machine
    "last_embedded_at": "...",    # null while PENDING; set on successful upsert
    "created_at": "...",
    "created_by_principal": "key-id-1"
}
```

### Tenancy

- `tenant_id` is on every entry, every query, every API call
- v0.1: single tenant `"default"` enforced at middleware (see ADR 0002)
- v0.2+: multi-tenant runtime + `X-Tenant-Override` header for principals with `role: admin`

### ACL (within a tenant)

- Each principal (HMAC key) has read/write namespace allowlist (gateway-deployed only; standalone has no auth)
- `agent_id` must match principal's allowed agent list (prevents Codex from impersonating Claude)

### Wire format relationship to mem0

Field names like `content`, `tags`, `metadata`, `created_at` are intentionally aligned with mem0's de facto schema, so the planned `mh import-mem0` script (v0.2 roadmap) is a near-mechanical mapping. memory-hall is **structurally richer** (`tenant_id`, `agent_id`, `references`, `sync_status`, `content_hash` have no mem0 equivalent), so the relationship is "compatible-on-import, not drop-in-replacement." See [ADR 0004](adr/0004-standardization-stance.md) for the broader stance on whether to push memory-hall as an industry spec.

## 7. Storage Layer

### SQLite (authoritative)
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT,
    references_json TEXT,
    metadata_json TEXT,
    sync_status TEXT NOT NULL DEFAULT 'pending',   -- pending | embedded | failed
    last_embedded_at TEXT,
    created_at TEXT NOT NULL,
    created_by_principal TEXT NOT NULL,
    UNIQUE (tenant_id, content_hash)               -- per-tenant dedup
);

CREATE INDEX idx_tenant_created ON entries(tenant_id, created_at DESC);
CREATE INDEX idx_tenant_agent ON entries(tenant_id, agent_id, created_at DESC);
CREATE INDEX idx_tenant_ns ON entries(tenant_id, namespace, created_at DESC);
CREATE INDEX idx_tenant_type ON entries(tenant_id, type, created_at DESC);
CREATE INDEX idx_sync_pending ON entries(sync_status) WHERE sync_status != 'embedded';

-- FTS5 virtual table for lexical search (Chinese-friendly tokenizer)
CREATE VIRTUAL TABLE entries_fts USING fts5(
    entry_id UNINDEXED,
    tenant_id UNINDEXED,
    content, summary, tags,
    tokenize='unicode61 remove_diacritics 0'
);
```

**Concurrency**: `aiosqlite` client + single uvicorn worker + `asyncio.Queue` single-writer pattern. Writes use `BEGIN IMMEDIATE`. Multi-process (Gunicorn) is not supported in v0.1 (would break the queue invariant).

### Vector store

**Protocol** — pluggable. Two implementations shipped:

- **sqlite-vec v0.1.6** (default, pinned) — embedded, zero external deps. Brute-force only; comfortable up to ~50K vectors. Beyond that, switch to qdrant.
- **qdrant** — production / multi-host / large-scale. Same SQLite stays as authoritative metadata store.

```python
class VectorStore(Protocol):
    def upsert(self, tenant_id: str, entry_id: str, vec: list[float]) -> None: ...
    def search(self, tenant_id: str, query_vec: list[float], k: int) -> list[tuple[str, float]]: ...
    def delete(self, tenant_id: str, entry_id: str) -> None: ...
```

### Embedder

**Protocol** — pluggable.

- **ollama** (default) — `bge-m3` recommended for multilingual quality (community-preferred for Chinese; reranker can lift recall 60%→85%)
- **openai** — `text-embedding-3-small` / `-large`
- **sentence-transformers** — local, no Ollama dependency

```python
class Embedder(Protocol):
    dim: int
    timeout_s: float = 2.0
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

**Failure handling**: 2-second hard timeout. On timeout/failure, write completes with `sync_status='pending'` and the API returns `202 Accepted` with `embedded: false`. The reindex worker (see §8) catches it later.

## 8. Failure modes & guarantees

| Failure | Behavior |
|---|---|
| Embedder slow / down (>2s) | Write succeeds; `sync_status='pending'`; API returns `202 Accepted, embedded: false`. Search degrades to lexical-only for that entry. |
| Embedder VRAM contention (>500ms but <2s) | Write succeeds; `sync_status='pending'`; future request triggers reindex. Avoids blocking under local LLM swap. |
| Vector store down | Same as above. |
| SQLite write contention | WAL + `BEGIN IMMEDIATE` + `busy_timeout=5000`. Single-writer queue prevents lock thrash. P99 stays under 100ms target on local deployments under 10 concurrent writers. |
| Auth fails | 401, no leak of tenant data. |
| Tenant isolation breach | Bug — covered by `tests/test_tenant_isolation.py` (the *first* integration test, runs against synthetic second tenant in v0.1). |
| Process crashes mid-write | Recoverable: SQLite row exists with `sync_status='pending'`. Reindex worker picks it up on restart. |
| Vector store drift (ghost row in vector store, missing in SQLite) | Search filters results by SQLite existence. Stale vectors swept by reindex worker. |

### SLA caveats

- **`P99 < 100ms`** applies to local-only deployment (Ollama on same host, sqlite-vec, single-tenant). Remote embedder + qdrant + multi-host has softer targets, primarily bounded by network and embedder latency. Production deployments should set their own SLA based on measured baseline.

## 9. Min vs Max deployment

### Min (single laptop, zero external deps)
- SQLite + sqlite-vec v0.1.6
- Ollama running locally (`ollama pull bge-m3`)
- Single process: `uv run memory-hall serve`
- No auth (suitable for local-only use; gateway-deploy if exposing beyond loopback)

### Max (multi-host home AI lab)
- SQLite (or Postgres v0.2+) on central host
- Qdrant on dedicated host (for shared vector store)
- Ollama on GPU host (DGX, RTX 3090, etc.) with model permanently loaded (`OLLAMA_KEEP_ALIVE=-1`)
- memory-gateway on always-on host (Mac mini, Raspberry Pi, etc.) — adds HMAC auth, ACL, staging, proposals
- Multiple agents (Claude on laptop via MCP, Codex CLI from anywhere, ChatGPT bot via webhook, …)

## 10. Search

### Hybrid (default)
1. SQLite FTS5 → BM25 score
2. Vector store cosine → semantic score
3. Reciprocal Rank Fusion (k=60) merge
4. Top K returned

### CJK short-query bias

For queries shorter than 5 characters, lexical (BM25) is weighted higher than semantic in the RRF merge. Short Chinese queries (e.g. "撞牆") often retrieve semantic noise; FTS5 unicode61 tokenization handles these directly.

### References traversal (v0.1, one-hop)

`GET /v1/memory/{entry_id}` returns the entry plus one hop of references (both directions). v2 may add recursive traversal.

## 11. Admin / maintenance

### `POST /v1/admin/reindex`
Sweeps entries with `sync_status != 'embedded'` and re-runs embedding + vector upsert. Also catches drifted vectors (rows missing from vector store but present in SQLite).

### `POST /v1/admin/audit`
Reports: total entries per tenant, per-namespace counts, sync_status histogram, content_hash collisions (should be zero).

Admin endpoints require `role: admin` principal (gateway deploy only; standalone has no admin protection — bind to localhost).

## 12. Roadmap

| Version | Scope |
|---|---|
| **v0.1** | Engine library, single-tenant runtime, SQLite + Ollama + sqlite-vec, HTTP + CLI, dogfood |
| **v0.2** | MCP server, qdrant adapter, multi-tenant runtime + admin override, `mh import-mem0` script, docker compose deployment story via memory-gateway |
| **v1.0** | Public release, docs site, 3–5 example agent integrations, benchmarks |
| **v2.0** | Optional async enrichment worker (fact extraction), more adapters (postgres, sentence-transformers default option) |

## 13. Validation plan (post v0.1)

- Bench bge-m3 + sqlite-vec + FTS5 hybrid on Maki's actual mem0 corpus exported via `mh import-mem0` (when available) — comparing recall/precision against mem0 baseline. Target: hybrid recall ≥ mem0 fact-search on top-20.
- Track Letta Code v0.23.x parallel agent memory isolation pattern; backport useful primitives if applicable.

## 14. Open questions (carried forward)

The major architectural disputes have been resolved by ADRs 0001–0003 and the Codex/Gemini reviews. Remaining smaller items:

1. ULID vs UUIDv7 for `entry_id` — sortability tradeoffs (currently ULID).
2. Reindex worker scheduling — cron / event-loop / external? (current plan: simple async task at startup + on-demand admin trigger; revisit if backlog grows).
3. Vector store deletion semantics — strict mirror or lazy delete? (current plan: strict mirror in v0.1; reindex worker reconciles).
