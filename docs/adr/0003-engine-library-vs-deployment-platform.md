# ADR 0003 — memory-hall (engine library) vs memory-gateway (deployment platform)

- **Status**: Accepted (Codex + Gemini independently converged on this split)
- **Date**: 2026-04-18

## Context

The author already maintains [memory-gateway](https://github.com/USERNAME/memory-gateway): a FastAPI service with HMAC auth, ACL, staging, proposals, and event-sourced governance APIs. Started in early 2026, paused mid-flight when its mem0-backed proxy turned out to inherit all of mem0's reliability problems.

Now building memory-hall as the replacement memory layer raises an obvious question: **build into memory-gateway, or fork a new repo?**

Both Codex (decision dissent review) and Gemini (architecture review) arrived independently at the same conclusion: **split responsibilities, don't fork**. memory-gateway already invested real engineering in the governance shell (HMAC/ACL/staging/proposals/events). That value should not be discarded. But its current `MemoryServiceProxy` hardcoded a specific downstream HTTP contract — it is *not* a thin "swap the backend" layer.

## Decision

Two separate, complementary repositories:

### `memory-hall` — engine library (this repo)

A self-contained Python package implementing the **memory engine**:

- Storage layer (SQLite + sqlite-vec)
- Embedder layer (Ollama / OpenAI / sentence-transformers)
- Search (FTS5 + RRF hybrid)
- CLI tool `mh`
- Optional standalone FastAPI server (for solo users who don't need governance)

Goals: install with `pip install memory-hall`, run with `uv run memory-hall serve`, no auth required for local-only setups, zero-config defaults that work.

### `memory-gateway` — deployment platform (existing repo)

A production deployment wrapper that:

- Depends on memory-hall as a library
- Adds the governance APIs (proposals / staging / events)
- Adds HMAC auth + ACL middleware
- Is what runs on Maki's Mac mini (and others' servers)

Goals: production-grade single-host deployment with governance and auth. Multi-tenant identity layer.

### Composition shape

```
memory-gateway (FastAPI app, governance + auth)
     │
     ↓ imports
memory-hall.engine (Python library)
     │
     ↓ uses
SQLite + sqlite-vec + Ollama
```

For solo users: `memory-hall serve` is enough (no auth, no governance overhead). For multi-user / production: deploy `memory-gateway`, which embeds memory-hall.

## Consequences

### Positive
- Existing memory-gateway investment (HMAC, ACL, staging, proposals) is preserved.
- memory-hall stays small, focused, and easy to install for OSS adoption.
- Solo and team deployments share the same engine, so behavior is consistent.
- Each repo has one job — fewer review surface conflicts.

### Negative
- Two repos to maintain instead of one.
- Cross-repo version coordination during early evolution (memory-gateway pinning a specific memory-hall version).
- Some duplication of HTTP handler shape (memory-hall ships a minimal FastAPI; memory-gateway has its own).

### Neutral
- The minimal FastAPI in memory-hall makes the package usable standalone, which is good for docs and adoption but means two FastAPI implementations to keep contract-aligned.

## Alternatives Considered

| Option | Why rejected |
|---|---|
| Build everything in memory-gateway | Couples engine and governance; governance investment delays engine development; harder to OSS the engine alone. |
| Fork memory-gateway → memory-hall, deprecate gateway | Throws away 2 months of governance engineering. Repo fragmentation without clear upside. |
| Build memory-hall as a microservice; memory-gateway calls over HTTP | Adds a network hop and a process to deploy. Memory layer should be in-process for the engine. |

## Implementation note

memory-gateway's `MemoryServiceProxy` (currently hardcoding mem0's HTTP contract via `MGW_MEMORY_SERVICE_URL`) is the migration point. It will be replaced with direct memory-hall library calls.

## References

- Codex Dissent on STG-020 (private record): `~/Documents/agent-council/codex-answer-mh-dissent.md` — independent finding that memory-gateway "is not as thin as STG-020 implied"
- Gemini Architecture Review (private): `~/Documents/agent-council/gemini-answer-mh-architecture.md` Q7 — "Extend memory-gateway. Do not fragment."
