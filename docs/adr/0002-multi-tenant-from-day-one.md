# ADR 0002 — Multi-tenant data model from day one (single-tenant runtime in v0.1)

- **Status**: Accepted (revised after Codex dissent + Gemini Q2 dispute, 2026-04-18)
- **Date**: 2026-04-18

## Context

memory-hall is initially built for one user (the author), but the intent is to release it for others to self-host — and for some users to grant access to friends, teammates, or their own bots.

Adding tenancy after the fact to a memory layer is expensive: every row, every index, every query, every cache key, every test fixture changes. Worse, leaks during retrofit can expose data across tenants in ways that are hard to detect.

**Codex (decision dissent)** challenged that committing to multi-tenant *runtime* in v0.1 dilutes the core goal of "stop the daily mem0 wall." **Gemini (architecture review Q2)** challenged that strict middleware-only resolution blocks legitimate admin / cross-tenant audit workflows.

Both points are valid. This revision separates **schema commitment** from **runtime enforcement scope**.

## Decision

### Schema: multi-tenant first-class from v0.1

`tenant_id` is on every table, every index, every query. No schema migration when multi-tenant runtime is enabled.

### Runtime: single-tenant `default` enforced in v0.1

v0.1 does **not** ship a tenant-creation API or per-tenant key issuance. Every authenticated principal maps to the `default` tenant. The middleware enforces this — no caller can write into another tenant.

Rationale: the engineering complexity of multi-tenant runtime (key issuance, ACL surface, tenant isolation testing breadth) is real, and adding it before the engine is proven on the author's own use case dilutes the core goal. v0.1 ships single-tenant; v0.2 lifts the lid.

### Admin override (v0.2+)

For batch imports, audit, and cross-tenant maintenance, principals with `role: admin` may pass an `X-Tenant-Override` header. The middleware honors it only when the principal claim contains `admin`; otherwise the override is silently ignored and the principal's own `tenant_id` is used.

This addresses Gemini's Q2 dispute without compromising the principal-derived-tenant safety property.

## Consequences

### Positive
- No schema migration when multi-tenant is enabled in v0.2.
- Tenant isolation testable from day one (`test_tenant_isolation.py` is the *first* integration test, per Max's recommendation).
- v0.1 stays focused on the engine's core promise.
- Admin override is a clean future extension, not a retrofit.

### Negative
- ~10–15% extra code in v0.1 that does no functional work yet (every CRUD path threads `tenant_id`, but only one value is ever used).
- `tests/test_tenant_isolation.py` will run against a synthetic second tenant in v0.1 to validate isolation, even though the runtime doesn't accept user-defined tenants.

### Neutral
- Single-tenant deployments incur trivial overhead (one fixed string in queries).

## Alternatives Considered

| Option | Why rejected |
|---|---|
| No tenant in v0.1 schema, retrofit later | Schema and middleware retrofit is the most error-prone path for memory layers. Cross-tenant leaks during retrofit are catastrophic. |
| Per-tenant SQLite database files | Operationally expensive (many SQLite files), no good answer for shared vector store. |
| URL path tenancy (`/v1/tenants/{tid}/memory/...`) | Caller-supplied tenant in URL is easy to spoof. Auth-principal-derived tenant is safer. |
| Strict middleware-only resolution, no admin override | Blocks legitimate admin workflows (Gemini Q2). Forces hacks (direct DB access, separate admin daemon) that are worse than a controlled override. |

## Implementation note

- Tenant resolution lives in `server/middleware/tenant.py`. It runs before route handlers.
- Routes never see raw `tenant_id` from the request body; they receive a validated value from `request.state.tenant_id`.
- v0.1 middleware: principal → `default` tenant, always. `X-Tenant-Override` header rejected with 400 if present.
- v0.2 middleware: principal → claim tenant. `X-Tenant-Override` honored only when claim has `role: admin`.

## References

- Codex Dissent (private): `~/Documents/agent-council/codex-answer-mh-dissent.md` — "multi-tenant from day one in v0.1 over-promises"
- Gemini Architecture Review (private): `~/Documents/agent-council/gemini-answer-mh-architecture.md` Q2 — admin override proposal
- Max Best Practice (private): `~/Documents/agent-council/max-answer-mh-research.md` Q4 — JWT claim pattern
