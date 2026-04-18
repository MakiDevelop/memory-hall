# memory-hall — Design

> Status: **DRAFT** v0.1 — pending Gemini architecture review + Codex dissent

## 1. Purpose

A self-hostable memory layer designed for people running **multiple AI agents** (Claude, Codex, Gemini, local LLMs, …) and wanting them to share state across sessions and tools.

Existing tools are wrong-shaped for this:

| Tool | Why it doesn't fit |
|---|---|
| **mem0 OSS** | Sync LLM call on every write → P99 unreliable past ~30s payloads. Fact extraction quality (community-audited 97.8% junk on long-running stores) and Claude-only MCP path. |
| **Letta** | Full agent runtime, not a memory layer — too opinionated. |
| **Zep / Graphiti** | Requires Neo4j or FalkorDB — heavier than a personal infra should bear. |
| **Mnemosyne** | Closest match, but TypeScript and lacks multi-agent identity layer. |

memory-hall fills the gap: **memory-layer-only, polyglot agents, zero-LLM write path.**

## 2. Goals

- **G1** — Write P99 < 100ms (zero LLM call on write path)
- **G2** — Cross-agent (Claude / Codex / Gemini / Max / Grok / local LLMs / humans)
- **G3** — Cross-session (open-ended pickup, not chat-bound)
- **G4** — Search quality matching or exceeding mem0 for "find what I wrote about X"
- **G5** — Multi-tenant from day one (`tenant_id` first-class, even if v0.1 runtime is single-tenant)
- **G6** — Min local (Ollama + sqlite-vec, zero external deps), max distributed (Qdrant + remote embedder host, multi-machine sync)
- **G7** — No middleware monopoly: HTTP REST is primary, MCP and CLI are convenience layers, none is the only legal path

## 3. Non-Goals

- **N1** — Sync fact extraction on write (optional async enrichment in v2)
- **N2** — Migrating existing mem0 stores (read-only legacy tool stays)
- **N3** — UI / TUI / graph visualization (future, separate package)
- **N4** — SaaS hosting (self-host first; SaaS only if community demand)
- **N5** — Replacing chat-bound memory (this is for agents, not consumer chat apps)

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  AGENTS (any combination)                                     │
│  Claude / Codex / Gemini / local LLM / humans / bots          │
└────┬───────────────┬──────────────┬───────────────────────────┘
     │ MCP           │ CLI (mh)     │ HTTP REST (curl/anything)
     ↓               ↓              ↓
┌──────────────────────────────────────────────────────────────┐
│  memory-hall server (FastAPI)                                 │
│  /v1/memory/{write, search, get}  /v1/proposals  /v1/events   │
│  middleware: hmac auth + tenant resolution + ACL              │
└────┬─────────────────────────────────────┬───────────────────┘
     │ write                               │ search
     ↓                                     ↓
┌──────────────────────┐    ┌─────────────────────────────────┐
│  Storage (Protocol)  │    │  Vector store (Protocol)         │
│  - sqlite (default)  │    │  - sqlite-vec (default, embedded)│
│  - postgres (v0.2+)  │    │  - qdrant (production / shared)  │
└──────────────────────┘    └─────────────────────────────────┘
                                          ↑
                                          │ embed-on-write
                                          │
                       ┌──────────────────────────────┐
                       │  Embedder (Protocol)          │
                       │  - ollama (default)           │
                       │  - openai                     │
                       │  - sentence-transformers      │
                       └──────────────────────────────┘
```

### Three entry points

1. **HTTP REST** — primary, language-agnostic
2. **MCP server** — wraps HTTP for Claude convenience
3. **CLI `mh`** — wraps HTTP for Codex / shell / non-MCP agents

All three hit the same backend. **No path is privileged** — any one being down doesn't break the others. (Deliberate violation of the "default = only" anti-pattern; see ADR 0002.)

## 5. Data Model

### Entry (v0.1 schema)
```python
{
    "entry_id": "01HX...",        # ULID, sortable, primary key
    "tenant_id": "default",       # multi-tenant partition (v0.1 always "default")
    "agent_id": "claude",         # who wrote this
    "namespace": "home",          # logical category (home/work/project:X/agent:X/shared)
    "type": "decision",           # episode/decision/observation/experiment/fact/note/question/answer
    "content": "...",             # raw text, recommended <8KB
    "summary": null,              # optional one-line summary
    "tags": ["mem0"],
    "references": ["01HX..."],    # graph edges to other entries
    "metadata": {},               # free-form
    "embedding": null,            # server-computed
    "created_at": "...",
    "created_by_principal": "key-id-1"
}
```

### Tenancy

- `tenant_id` is on every entry, every query, every API call
- v0.1: single tenant `"default"` enforced by middleware
- v0.2+: multi-tenant validation (HMAC key carries tenant claim)
- Cross-tenant reads/writes always blocked by middleware before reaching storage

### ACL (within a tenant)

- Each principal (HMAC key) has read/write namespace allowlist
- `agent_id` must match principal's allowed agent list (prevents Codex from impersonating Claude)
- Maki's principal: full access

## 6. Storage Layer

### SQLite (authoritative)
```sql
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT,
    references_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    created_by_principal TEXT NOT NULL
);

CREATE INDEX idx_tenant_created ON entries(tenant_id, created_at DESC);
CREATE INDEX idx_tenant_agent ON entries(tenant_id, agent_id, created_at DESC);
CREATE INDEX idx_tenant_ns ON entries(tenant_id, namespace, created_at DESC);
CREATE INDEX idx_tenant_type ON entries(tenant_id, type, created_at DESC);

-- FTS5 virtual table for lexical search (Chinese-friendly tokenizer)
CREATE VIRTUAL TABLE entries_fts USING fts5(
    entry_id UNINDEXED,
    tenant_id UNINDEXED,
    content, summary, tags,
    tokenize='unicode61 remove_diacritics 0'
);
```

### Vector store

**Protocol** — pluggable. Two implementations shipped:

- **sqlite-vec** (default) — embedded, zero external deps. Single-file deployment.
- **qdrant** — production / multi-host / large-scale. Same SQLite stays as authoritative metadata store.

```python
class VectorStore(Protocol):
    def upsert(self, tenant_id: str, entry_id: str, vec: list[float]) -> None: ...
    def search(self, tenant_id: str, query_vec: list[float], k: int) -> list[tuple[str, float]]: ...
    def delete(self, tenant_id: str, entry_id: str) -> None: ...
```

### Embedder

**Protocol** — pluggable.

- **ollama** (default) — `bge-m3` recommended for multilingual quality
- **openai** — `text-embedding-3-small` / `-large`
- **sentence-transformers** — local, no Ollama dependency

```python
class Embedder(Protocol):
    dim: int
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

## 7. API (v0.1)

### Write
```http
POST /v1/memory/write
Authorization: HMAC <key-id>:<sig>
X-Nonce: <random>
{
  "agent_id": "claude",
  "namespace": "home",
  "type": "decision",
  "content": "...",
  "tags": ["governance"],
  "metadata": {"importance": 4}
}
→ 201 { "entry_id": "01HX...", "embedded": true }
```

### Search (hybrid by default)
```http
POST /v1/memory/search
{
  "query": "mem0 撞牆",
  "namespace": ["home", "shared"],
  "agent_id": null,
  "type": ["decision", "experiment"],
  "limit": 20,
  "mode": "hybrid"        // lexical | semantic | hybrid
}
→ 200 { "results": [...], "total": 47 }
```

Hybrid combines BM25 (FTS5) + cosine (vector store) via Reciprocal Rank Fusion.

### Other
- `GET /v1/memory/{entry_id}` — single entry + references graph
- `GET /v1/memory?since=...&namespace=...` — list with filters
- `POST /v1/memory/{entry_id}/link` — add a reference edge
- `POST /v1/proposals` (governance, optional) — staged decisions before commit

## 8. Failure modes & guarantees

| Failure | Behavior |
|---|---|
| Embedder down | Write succeeds, marks `embedded=false`, background worker retries. Search degrades to lexical-only. |
| Vector store down | Same as above. |
| SQLite write contention | WAL mode + single-writer queue. P99 should stay under 100ms even under 10 concurrent writers. |
| Auth fails | 401, no leak of tenant data |
| Tenant isolation breach | Bug — covered by `test_tenant_isolation.py` test suite |

## 9. Min vs Max deployment

### Min (single laptop, zero external deps)
- SQLite + sqlite-vec
- Ollama running locally (`ollama pull bge-m3`)
- One process: `uv run memory-hall serve`

### Max (multi-host home AI lab, e.g. Maki's setup)
- SQLite (or Postgres v0.2+) on central host
- Qdrant on dedicated host (for shared vector store)
- Ollama on GPU host (DGX, RTX 3090, etc.) with model permanently loaded
- memory-hall server on always-on host (Mac mini, Raspberry Pi, etc.)
- Multiple agents (Claude on laptop, Codex CLI from anywhere, ChatGPT bot via webhook, …)

## 10. Roadmap

| Version | Scope |
|---|---|
| **v0.1** | Single-tenant runtime, SQLite + Ollama, HTTP + CLI, Maki dogfood |
| **v0.2** | MCP server, Qdrant adapter, multi-tenant validation, docker compose |
| **v1.0** | Public release, docs site, 3-5 example agent integrations |
| **v2.0** | Optional async enrichment worker (fact extraction), more adapters |

## 11. Open questions (for Codex Dissent + Gemini Review)

1. Is tenant resolution at the middleware layer the right architectural seam, or should `tenant_id` be implicit-from-key throughout?
2. SQLite writer queue vs. WAL-only — has anyone benchmarked at our scale?
3. Should the vector store be a strict mirror of SQLite, or allowed to drift (e.g. lazy delete)?
4. ULID vs UUIDv7 for `entry_id` — sortability tradeoffs?
5. Is `references` graph queryable in v0.1, or just stored for later traversal?
