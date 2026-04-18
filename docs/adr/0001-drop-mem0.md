# ADR 0001 — Drop mem0 as primary memory store

- **Status**: Proposed (pending Codex dissent)
- **Date**: 2026-04-18

## Context

The author's prior setup used [mem0](https://github.com/mem0ai/mem0) (OSS, self-hosted) as the central memory layer for a multi-agent personal infrastructure. After three months of daily use with multiple agents (Claude, Codex, Gemini, local LLMs) and a few hundred memory entries, the following pattern hardened:

1. **Daily write timeouts.** Long-form Chinese payloads (~500+ chars) routinely exceeded 30s on `Memory.add()` because mem0 performs synchronous LLM-driven fact extraction on every write. With existing entries growing, dedup added more LLM calls per write.
2. **Hard transport ceiling.** Claude Code's MCP client enforces a ~60–120s tool-call timeout (depending on transport, see [anthropics/claude-code#7575](https://github.com/anthropics/claude-code/issues/7575) and adjacent issues). `MCP_TIMEOUT` env vars are not honored above ~60s.
3. **Sticky failure cascades.** A circuit-breaker layer in front of mem0 would open after repeated timeouts, then fail to recover because half-open probes used real long payloads (which kept timing out).
4. **Quality concerns.** The mem0 community has documented quality issues with extracted facts on long-running self-hosted stores ([mem0ai/mem0#4573](https://github.com/mem0ai/mem0/issues/4573)).
5. **Single-vendor coupling.** mem0 is reachable from Claude (via MCP) but awkward from other agents (Codex, Gemini, plain shell). Cross-agent use required a wrapper layer that itself became a single point of failure.

The architectural root is not a tunable: mem0's design assumes the caller can wait on synchronous LLM extraction. For a memory layer that needs to be reliable and cross-agent, that assumption doesn't hold.

## Decision

Build a new memory layer (this project, `memory-hall`) with:

- **Zero-LLM write path** — write is `INSERT` + embedding only. No synchronous fact extraction.
- **Cross-agent by primary contract** — HTTP REST as the canonical interface; MCP and CLI as convenience wrappers. None is the "only legal" path.
- **Multi-tenant data model from v0.1** — every row, every query carries `tenant_id`. Single-tenant runtime initially, but the schema doesn't need migration to scale.
- **Optional async enrichment (v2+)** — if structured fact extraction is desired, it runs out-of-band and never blocks a session.

`mem0` stays installed as a read-only legacy tool for querying historical entries. No new writes go to it.

## Consequences

### Positive
- Daily write failures eliminated.
- Cross-agent memory becomes a real architectural primitive, not a Claude-side wrapper.
- Multi-tenant by design — releasable to others without refactoring.
- Aligns with [middleware-not-monopoly](../../docs/design.md#three-entry-points) principle: no privileged path.

### Negative
- Loss of automatic fact extraction. "Find facts about X" degrades to "find entries mentioning X." For most retrieval use cases (cross-session pickup, decision audit trail), this is acceptable; for some it is not.
- Existing mem0 entries are not migrated. Users who need to query both must use both tools.
- Maintaining a memory layer is now an in-house responsibility.

### Neutral
- New project to maintain, but the surface area is much smaller than mem0 and the failure modes are inspectable (no LLM in the write path).

## Alternatives Considered

| Option | Why rejected |
|---|---|
| Patch mem0 timeouts (raise deadlines, add breaker reset, etc.) | Architectural ceiling unchanged. Would only reduce frequency, not eliminate failures. |
| Switch to [Letta](https://github.com/letta-ai/letta) | Full agent runtime, not a memory layer. Too opinionated and heavy. |
| Switch to [Zep](https://github.com/getzep/zep) / [Graphiti](https://github.com/getzep/graphiti) | Higher retrieval quality but requires Neo4j or FalkorDB infrastructure — too heavy for "min local" goal. |
| Use [Mnemosyne](https://github.com/Mnemosyne-Project) | Closest design philosophy, but TypeScript and lacks a multi-agent identity layer. |
| Wrap mem0 with a fire-and-forget queue | Caller no longer knows write outcome — silent data loss risk. |

## Open Questions

- Should the project ship a one-shot `mem0 → memory-hall` exporter for users with existing mem0 data? (Currently: no, by ADR scope.)
- Should the optional v2 enrichment worker be in-tree or a separate package?

## References

- Incident record (private): `STG-019` — diagnosis of mem0 write timeouts
- Decision record (private): `STG-020` — full proposal contract with risk analysis
