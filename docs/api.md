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

### Minimal token mode (single-tenant deployment)
When `MH_API_TOKEN` is set, every request (except `GET /v1/health`) must carry:
```
Authorization: Bearer <MH_API_TOKEN>
```
Missing or wrong token → `401`. `/v1/health` stays public so external uptime probes and the in-image HEALTHCHECK don't need credentials. Rationale and scope limits in [ADR 0007](adr/0007-minimal-token-auth.md). This is **not** a replacement for the production HMAC mode below — it's a local-network deployment shim.

### Admin gate (two-tier bearer, optional)
When `MH_ADMIN_TOKEN` is set in addition to `MH_API_TOKEN`:
- `/v1/admin/*` paths require `Authorization: Bearer <MH_ADMIN_TOKEN>` — the regular `MH_API_TOKEN` is **rejected** on admin paths
- The regular `MH_API_TOKEN` continues to gate non-admin paths (`/v1/memory/*`)
- `MH_ADMIN_TOKEN` does **not** grant access to non-admin paths (least privilege both ways)
- `/v1/health` remains public

When `MH_ADMIN_TOKEN` is unset, admin paths fall back to `MH_API_TOKEN` (backward compatible with ADR 0007). Operators are encouraged to also lock `/v1/admin/*` to specific devices via Tailscale ACL as defense-in-depth. Rationale in [ADR 0009](adr/0009-admin-gate.md).

### Future HMAC mode (planned via `memory-gateway`, not implemented in this repo yet)
The long-term production mode is the HMAC scheme below:
- `Authorization: HMAC <key-id>:<signature>` header
- `X-Nonce: <random>` header (replay protection)
- `tenant_id` resolved from the auth principal, never accepted from the request body
- Bind address can be `0.0.0.0`

Today, this repo only implements the two modes above: dev / standalone and minimal token mode. The examples below omit auth headers for brevity; if `MH_API_TOKEN` is set, add `Authorization: Bearer <MH_API_TOKEN>` and do not send `X-Nonce`.

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
