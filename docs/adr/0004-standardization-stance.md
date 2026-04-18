# ADR 0004 — Standardization stance: implementation first, spec later

- **Status**: Accepted
- **Date**: 2026-04-18

## Context

There is no industry standard for cross-agent memory layers as of April 2026. mem0 is a *de facto reference implementation* with a recognizable schema, but no specification body has formalized memory format / wire protocol / query semantics. MCP standardized tool calling in 2024 but explicitly did not cover memory.

Possible reasons (informal):
- AI agents themselves only became production-shaped in 2024–2025; standards usually emerge after 5–10 years of practice.
- Vendors (mem0 / Letta / Zep) gain from differentiation, not commoditization.
- Frameworks (LangChain / AutoGen / CrewAI) ship their own memory abstractions as product surface.
- Several core design questions remain unresolved: episodic vs semantic split, temporal model (graph vs flat), identity model (user/agent/tenant/session permutations), search semantics (vector / hybrid / graph).
- Anthropic and OpenAI haven't pushed it, possibly because their own memory products (Claude Memory, ChatGPT Memory) are still evolving.
- The audience that would push such a standard — multi-agent power users — is small and not yet organized.

This presents both a risk (we may diverge into yet another snowflake schema) and an opportunity (a clean, well-dogfooded OSS implementation could become the de facto standard, the way Anthropic's MCP did for tool calling).

## Decision

**v0.1 → v1.0: implementation-first, no spec push.**

- Optimize the schema, API, and behavior for the author's actual multi-agent use case.
- Keep field names compatible with mem0 conventions where it costs nothing (`content`, `tags`, `metadata`, `created_at`), so import paths stay easy.
- Do not freeze the schema; expect breaking changes between v0.x releases.
- Do not engage with formal standardization bodies (no W3C, no IETF, no OpenAI/Anthropic spec proposals).

**v1.0 release gate:**

- Schema declared stable.
- Wire format documented as a separate `memory-hall-spec/` reference.
- Migration guide written.

**Post-v1.0 (only if traction warrants):**

- Open an RFC inviting other agent frameworks (Letta, OpenClaw, CrewAI, AutoGen, …) to comment on or align with memory-hall's wire format.
- Spin out `memory-hall-spec/` as a separate repository if community interest justifies it.
- Position memory-hall as one reference implementation of the spec, not the spec.

## Consequences

### Positive
- v0.1–v1.0 development isn't slowed by spec-process overhead.
- Schema can iterate freely as real cross-agent use surfaces unknowns.
- If memory-hall fails to gain traction, no standardization debt to clean up.
- If it succeeds, the path to "de facto standard" is open without premature commitment.

### Negative
- v0.x users may experience breaking changes; mitigated by clear changelog and migration scripts.
- We may rediscover problems that a standards process would have surfaced earlier (acceptable cost given how early the space is).

### Neutral
- The decision itself is reversible. If a credible spec emerges from another source pre-v1.0, we'll evaluate adopting it.

## Alternatives Considered

| Option | Why rejected |
|---|---|
| Push for industry spec from v0.1 | Too early; no co-signers; diverts attention from the engine. |
| Adopt mem0's schema as the de facto standard now | mem0's schema has known quality problems (see ADR 0001); adopting it codifies the problems. |
| Wait for Anthropic / OpenAI to lead | They've had two years to do so and haven't. Not a reliable plan. |
| Define spec in parallel with implementation | Doubles surface area; spec drift from implementation is the normal failure mode here. |

## Operational implications

- README and ADRs **may** describe memory-hall's schema as "structurally richer than mem0's, compatibly named" but **must not** describe it as a "standard."
- No `memory-hall-spec/` repo is created until v1.0 release decision.
- Issues and PRs requesting "spec compatibility" with other tools are accepted on a case-by-case basis if the change improves memory-hall's own quality; rejected if they would constrain the engine's evolution.

## References

- ADR 0001 — Drop mem0 as primary memory store
- ADR 0003 — Engine library vs deployment platform split
