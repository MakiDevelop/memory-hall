# ADR 0007 — Minimal Token auth (single-tenant deployment shim)

- **Status**: Accepted
- **Date**: 2026-04-23
- **Related**: ADR 0005 (v0.2 Minimum Viable Contract), external rule `rules/agent-security-hygiene.md` S2.1

## Context

Until this change, memory-hall's HTTP surface was unauthenticated. The canonical deployment (Mac mini on a Tailscale tailnet, `:9100`) trusts the network: any process on the tailnet could write to, read from, or list any namespace. Maki's external rule book (`rules/agent-security-hygiene.md` S2.1) calls for an HMAC-signed principal model: `X-Signature: sha256=...` + `X-Timestamp: ...` + replay window + per-key-id rotation. That is the right long-term answer. It is also 1–2 days of work and requires every caller (ops-hub `fleet/memhall.py`, four Claude skill `SKILL.md` files, embedded-mode Python) to sign every request.

The trigger for acting *now* was a five-day review cycle (2026-04-18 → 2026-04-23) in which the seven-agent council explicitly re-prioritised operational reliability over ACL depth. Scout-2 (SuperGrok) reported the 2026-Q2 community consensus that local-first deployments frame the threat model as "keep data off the vendor cloud," not "lock down in-tailnet ACLs." Mac mini + Tailscale already delivers the first; the second is a smaller, local, later problem.

## Decision

**Add a single shared-secret Bearer token as the default auth for production deployments. Leave it off for dev.**

- New config field: `Settings.api_token: str | None = None` (`MH_API_TOKEN` env).
- When unset: server behaves exactly as before — no auth, backward compatible.
- When set: every request must carry `Authorization: Bearer <token>` or the server returns `401`. Enforced by an `@app.middleware("http")` layer in `create_app()` (see `src/memory_hall/server/app.py`) using `hmac.compare_digest` for constant-time comparison.
- **`GET /v1/health` is intentionally exempt.** External uptime monitors and the in-image Docker `HEALTHCHECK` probe it without credentials; the endpoint returns only embedder / storage status, no entries.

This is a **deployment shim**, explicitly not the full S2.1 scheme. It is named "minimal token mode" in `docs/api.md` and sits between "dev / standalone mode" (no auth) and the not-yet-implemented "production mode via `memory-gateway`" (HMAC).

## Consequences

### Gains

- **Tailnet no longer implies write access.** A compromised container or a misbehaving script on the tailnet without the token can no longer silently write to shared / work namespaces.
- **2–3 hours to roll out end-to-end.** Server-side + six known callers (one Python client, four skill files, one CLAUDE.md example). Fits an afternoon.
- **Zero config for OSS users.** Leaving `MH_API_TOKEN` unset preserves the "`git clone && docker compose up`" experience for external adopters — the engine's "stay small" philosophy (README) is intact.
- **Reversible.** Unsetting the env returns to pre-0.2.1 behaviour. No migration.

### Costs

- **Shared secret, not per-agent principal.** Every caller using the same token is indistinguishable. Audit logs will not differentiate "ops-hub wrote this" from "/wrap-up skill wrote this" beyond what `agent_id` in the payload says — and that field is self-declared, not auth-derived.
- **No replay protection.** A captured request can be replayed until the token is rotated. On a Tailscale tailnet with TLS terminating inside the service, the capture surface is small but non-zero.
- **No key rotation.** Rotating means edit env + restart container + update six callers in one go. Acceptable at the current ~3-caller, single-operator scale; painful if callers proliferate.

### Non-goals

- Not a replacement for S2.1 HMAC. When a second real operator adopts memory-hall, or when a caller needs per-request signing for audit reasons, we move to full HMAC (probably as a separate auth mode, not a replacement — allow both).
- Not fine-grained ACL. `namespace` policing still relies on caller honesty.
- Not TLS termination. Handled by the Tailscale tailnet / the operator's reverse proxy.

## Alternatives considered

### A. Full HMAC now (the S2.1 spec)

Rejected for sequencing, not for design. S2.1 is the destination. But 1–2 days of work against 6 hours of active operational risk (no auth at all) made this the wrong first step. The minimal token buys time to ship S2.1 properly rather than a half-done version.

### B. IP allowlist via Tailscale ACL

Rejected: Tailscale ACLs are coarse-grained (device, not process) and leak memory-hall's trust model into infra config. The service should be self-defending regardless of who wired up the tailnet.

### C. mTLS

Rejected: per-caller cert provisioning on six callers, cert rotation pipeline, and a PKI chain for a single-operator deployment. Complexity dwarfs the threat.

## Implementation summary

- `src/memory_hall/config.py` — add `api_token` field (`None` default).
- `src/memory_hall/server/app.py` — new `require_api_token` middleware, runs before `enforce_write_content_limit`. Bypasses `/v1/health`. Uses `hmac.compare_digest`.
- `tests/test_auth.py` — 7 tests: unset-bypass, missing-header, wrong-scheme, wrong-token, valid-token, health-public, search-requires-token.
- `.env.example` — documented `MH_API_TOKEN` with generation instruction (`openssl rand -hex 32`).
- `docker-compose.yml` — passes `MH_API_TOKEN` through (empty default).
- `docs/api.md` — new "Minimal token mode" section between dev and HMAC-production.

Total: ~90 lines across 6 files. Existing test suite unaffected (the one pre-existing `test_vec0` failure on macOS MBP dev machines is platform-specific, not introduced here).

## Sunset criteria

Revisit this ADR when any of the following happens:

1. A second operator (not Maki) deploys memory-hall in a networked production setup.
2. A caller needs per-request provenance guarantees that self-declared `agent_id` cannot provide.
3. The token leaks (in which case this ADR is why we don't have rotation infra, and that's the first thing to build).

Until then, this is the auth story.
