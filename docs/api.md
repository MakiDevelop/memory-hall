# API Reference (v0.1 — DRAFT)

> Final OpenAPI spec will be auto-generated from FastAPI handlers.
> This document tracks the intended contract until then.

Base URL: `http://localhost:9000`

## Auth modes

memory-hall has **two operating modes** depending on how it's deployed:

### Dev / standalone mode (default for `memory-hall serve`)
- Listens on `127.0.0.1` only
- No `Authorization` header required
- No `X-Nonce` required
- `tenant_id` is always `"default"`
- Suitable for: single-user local use, integration testing, examples

### Production mode (when deployed via `memory-gateway`)
All endpoints require:
- `Authorization: HMAC <key-id>:<signature>` header
- `X-Nonce: <random>` header (replay protection)
- `tenant_id` resolved from the auth principal, never accepted from the request body
- Bind address can be `0.0.0.0`

The examples below show production-mode requests. For dev mode, drop the `Authorization` and `X-Nonce` headers.

## Memory

### `POST /v1/memory/write`

Write a single entry.

**Body**:
```json
{
  "agent_id": "claude",
  "namespace": "home",
  "type": "decision",
  "content": "STG-020: drop mem0",
  "summary": "drop mem0 as primary store",
  "tags": ["governance"],
  "references": [],
  "metadata": {"importance": 4}
}
```

**Response 201**:
```json
{
  "entry_id": "01HXABC...",
  "embedded": true,
  "indexed_at": "2026-04-18T17:30:00Z"
}
```

### `POST /v1/memory/search`

Hybrid lexical + semantic search.

**Body**:
```json
{
  "query": "mem0 timeout",
  "namespace": ["home", "shared"],
  "agent_id": null,
  "type": ["decision", "experiment"],
  "tags": null,
  "limit": 20,
  "mode": "hybrid"
}
```

`mode`: `lexical` | `semantic` | `hybrid` (default).

**Response 200**:
```json
{
  "results": [
    {
      "entry_id": "01HX...",
      "score": 0.87,
      "score_breakdown": {"bm25": 0.91, "semantic": 0.83},
      "entry": { ... }
    }
  ],
  "total": 47
}
```

### `GET /v1/memory/{entry_id}`

Single entry with reference graph (one hop).

### `GET /v1/memory?since=&until=&namespace=&agent_id=&type=&limit=&cursor=`

Paginated list.

### `POST /v1/memory/{entry_id}/link`

Add a reference edge.

**Body**: `{"target_entry_id": "01HX...", "relation": "supersedes"}`

## Governance (optional)

### `POST /v1/proposals`
Stage a decision before commit.

### `GET /v1/proposals?status=pending`
List staged proposals.

### `POST /v1/proposals/{id}/approve` / `reject` / `deprecate`

## Audit

### `GET /v1/events?since=&type=`
Append-only event log of all writes / approvals.

## Health

### `GET /v1/health`
Returns embedder + vector store + storage status.
