# Architecture Decision Records

Numbered, immutable records of significant design choices. Append new entries; never rewrite past ones — supersede via a new ADR if needed.

| # | Title | Status |
|---|---|---|
| [0001](0001-drop-mem0.md) | Drop mem0 as primary memory store | Accepted (2026-04-18) |
| [0002](0002-multi-tenant-from-day-one.md) | Multi-tenant data model from day one (single-tenant runtime in v0.1) | Accepted (2026-04-18) |
| [0003](0003-engine-library-vs-deployment-platform.md) | memory-hall (engine library) vs memory-gateway (deployment platform) | Accepted (2026-04-18) |
| [0004](0004-standardization-stance.md) | Standardization stance: implementation first, spec later | Accepted (2026-04-18) |
| [0005](0005-v0.2-minimum-viable-contract.md) | v0.2 Minimum Viable Contract (production-facing freeze) | Accepted (2026-04-19) |
| [0006](0006-http-embedder-embed-queue-isolation.md) | HttpEmbedder: embed path isolation from LLM queue | Accepted (2026-04-20) |
| [0007](0007-minimal-token-auth.md) | Minimal Token auth (single-tenant deployment shim) | Accepted (2026-04-23) |
| [0008](0008-personal-pki-lightweight-stance.md) | memhall 是 personal PKI，輕量 > 完整 | Accepted (2026-04-28) |
| [0009](0009-admin-gate.md) | Admin gate（two-tier bearer，不做 HMAC） | Accepted (2026-04-28) |

## Format

Each ADR has: Context, Decision, Consequences, Alternatives Considered. Optional: Open Questions.
