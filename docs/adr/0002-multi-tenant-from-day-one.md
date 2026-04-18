# ADR 0002 — Multi-tenant from day one

- **Status**: Proposed
- **Date**: 2026-04-18

## Context

memory-hall is initially built for one user (the author), but the intent is to release it for others to self-host — and for some users to grant access to friends, teammates, or their own bots.

Adding tenancy after the fact to a memory layer is expensive: every row, every index, every query, every cache key, every test fixture changes. Worse, leaks during retrofit can expose data across tenants in ways that are hard to detect.

## Decision

`tenant_id` is a first-class field from v0.1:

- Present on every row in every table
- Required in every API call (resolved from the auth principal, not user-supplied)
- Present in every index (`idx_tenant_*`)
- Filtered at the middleware layer before queries reach storage
- Covered by a dedicated test suite (`test_tenant_isolation.py`)

The v0.1 runtime ships with a single tenant `"default"` and an enforcement that all auth keys map to it. v0.2 lifts this to allow multiple tenants per deployment.

## Consequences

### Positive
- No schema migration when multi-tenant is enabled.
- Tenant isolation testable from day one.
- `tenant_id` becomes a natural sharding key for future scale-out.

### Negative
- ~20% extra code in v0.1 that does no functional work yet (every CRUD path touches tenant resolution).
- Slightly more verbose internal APIs (`store.write(tenant_id, entry)` vs `store.write(entry)`).

### Neutral
- Single-tenant deployments incur trivial overhead (one fixed string in queries).

## Alternatives Considered

| Option | Why rejected |
|---|---|
| Single-tenant v0.1, retrofit later | Schema and middleware retrofit is the most error-prone path for memory layers. Cross-tenant leaks during retrofit are catastrophic. |
| Per-tenant database files | Operationally expensive (many SQLite files), no good answer for shared vector store. |
| URL path tenancy (`/v1/tenants/{tid}/memory/...`) | Caller-supplied tenant in URL is easy to spoof. Auth-principal-derived tenant is safer. |

## Implementation note

Tenant resolution happens in `server/middleware/tenant.py` before requests reach route handlers. Routes never see raw `tenant_id` from the request body; they receive a validated value from `request.state.tenant_id`.

## References

- [middleware-not-monopoly](../../README.md) — the same anti-coupling principle applies inside as well as outside
