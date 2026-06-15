# memory-hall ↔ AMH Integration

This document is the **shared contract** between the storage engine and the governance protocol.

| Repo | Role |
|------|------|
| [memory-hall](https://github.com/MakiDevelop/memory-hall) (this repo) | SQLite + sqlite-vec + hybrid search |
| [agent-memory-hall](https://github.com/MakiDevelop/agent-memory-hall) | AMH protocol, MCP, write-gate, revoke, audit |

Mirror doc: [agent-memory-hall/docs/INTEGRATION.md](https://github.com/MakiDevelop/agent-memory-hall/blob/main/docs/INTEGRATION.md)

---

## Philosophy

memory-hall **stays small**: store, index, search. It does not implement:

- namespace write-gate / caller ACL
- anti-Ouroboros (`source_tier` chains)
- revoke governance
- MCP tool surface (examples only)

That is **[Agent Memory Hall (AMH)](https://github.com/MakiDevelop/agent-memory-hall)**. Agents should call `amh write` / `amh_read` MCP, not raw HTTP — unless they only need search or batch import.

```
Agents → AMH (governance) → MemhallStore adapter → memory-hall HTTP :9100
```

Direct HTTP/CLI (`mh`, curl) remains valid for **engine-level** tools, benchmarks, and emergency ops.

---

## What AMH stores in our rows

When `--store memhall` is used, the adapter maps AMH records into standard write payloads:

```json
{
  "agent_id": "claude",
  "namespace": "project:agent-memory-hall",
  "type": "episode",
  "content": "…",
  "tags": ["amh"],
  "metadata": {
    "amh_version": "0.1",
    "amh_status": "active",
    "source_type": "agent",
    "source_ref": "session:…",
    "source_tier": "llm_derived",
    "valid_until": null,
    "supersedes": null
  }
}
```

Lifecycle states: `metadata.amh_status` ∈ `active` | `superseded` | `revoked` | `expired`.

AMH default reads filter inactive statuses in the protocol layer. Engine search **does not** filter by `amh_status` (deferred per council — stays AMH read layer).

**Phase 3 CI:** `scripts/contract_test_server.py` + `tests/test_amh_contract.py`; AMH repo runs `integration-memhall.yml` on `main`.

---

## Engine responsibilities for AMH

| AMH need | memory-hall capability | Status |
|----------|------------------------|--------|
| Persist content | `POST /v1/memory/write` | ✅ |
| Fetch by id | `GET /v1/memory/{entry_id}` | ✅ |
| Hybrid recall | `POST /v1/memory/search` | ✅ |
| Supersede graph | `POST /v1/memory/{id}/link` | ✅ AMH 0.6.4+ |
| Hash lookup (engine) | `GET /v1/memory/by-hash` | ✅ Phase 3 |
| Hash lookup (AMH BLAKE3) | `GET /v1/memory/by-amh-hash` | ✅ Phase 3 |
| Stable id on write | `entry_id` in response (ULID) | ✅ server-side |
| Client-chosen id | `entry_id` in request body | ❌ not in schema |
| Metadata update without content change | `PATCH /v1/memory/{id}` | ✅ shallow merge (2026-06-15) |
| AMH BLAKE3 hash | `metadata.amh_content_hash` | ✅ on AMH writes |

---

## Dedup interaction (critical)

Insert dedup: `UNIQUE (tenant_id, content_hash)` where `content_hash = sha256(content)`.

On duplicate content, `insert_entry` returns the **existing** row and does **not** update `metadata_json`.

**Impact on AMH revoke:** AMH tries to re-write the same content with `amh_status: revoked`. Engine returns 200 dedup hit; status stays `active`.

**Fixed (2026-06-15):** `PATCH /v1/memory/{entry_id}` shallow-merges `metadata` (allowlist: `amh_*`, `revoked_by`, `revoked_reason`). AMH revoke/supersede use this path; supersede graph via `POST …/link`.

---

## ID model

| System | ID format | Allocated by |
|--------|-----------|--------------|
| memory-hall | ULID (26 chars) | `new_entry_id()` on every write |
| AMH | UUID v4 | `writeMemory()` before adapter call |

Today the adapter sends `entry_id` in the POST body but **`WriteMemoryRequest` ignores it**. AMH callers may hold a UUID that does not exist in the DB.

**Planned fix:** optional `entry_id` on write (ADR), or AMH must adopt response `entry_id` (AMH-side fix, Phase 1).

---

## Search vs list for handoff

AMH `read` without `--text` maps to search with namespace as query. For **latest episode**, prefer:

- `GET /v1/memory?namespace=…&type=episode&limit=N` (created_at DESC)

See [agent-integration.md](agent-integration.md) § Handoff retrieval.

---

## Auth

AMH adapter sends `Authorization: Bearer $MH_API_TOKEN`. Same as Path B in agent-integration.md.

`/v1/admin/*` is not used by AMH adapter. Governance stays in AMH process.

---

## What we will not add here

Per [ADR 0003](adr/0003-engine-library-vs-deployment-platform.md):

- Per-agent namespace ACL (AMH `caller_namespace`)
- Write-gate / anti-Ouroboros
- MCP server in core package
- Mandatory AMH dependency

Optional future: `metadata` schema docs for `amh_*` keys in `docs/design.md`.

---

## Paired release checklist

With each AMH release that touches `MemhallStore`:

- [ ] Write → read by returned `entry_id`
- [ ] Metadata PATCH or dedup merge for revoke
- [ ] Link API used on supersede (AMH side)
- [ ] Both INTEGRATION.md files updated
- [ ] Note `integration:memhall-amh` in CHANGELOG

---

## Related

- [README.md](../README.md) — quickstart
- [api.md](api.md) — HTTP contract
- [agent-integration.md](agent-integration.md) — Codex / Gemini path selection
- [AMH ARCHITECTURE](https://github.com/MakiDevelop/agent-memory-hall/blob/main/packages/core/ARCHITECTURE.md)