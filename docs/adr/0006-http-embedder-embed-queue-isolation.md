# ADR 0006 — HttpEmbedder: embed path isolation from LLM queue

- **Status**: Accepted
- **Date**: 2026-04-20
- **Related**: ADR 0005 (v0.2 Minimum Viable Contract), incident log `docs/operations/incident-2026-04-20-embed-queue.md`

## Context

Until 0.2.0, memory-hall had exactly one embedder backend: `OllamaEmbedder` against `MH_OLLAMA_BASE_URL`. On 2026-04-20 the production seven-agent stack hit an incident that exposed a structural problem with this:

**Ollama is a shared runner pool**. When LLM clients hammer `/api/generate` or `/v1/chat/completions` with multi-GB models (qwen3-vl, qwen3.5:35b-a3b, etc.), Ollama's scheduler evicts/loads models to fit GPU+system memory. `bge-m3` (small, fast, embed-only) gets starved: every embed request triggers a cold load that never wins against the LLM traffic.

Observed symptoms on the day:
- `/v1/health` returned `embedder: degraded` continuously (1s probe timeout, but cold-load of bge-m3 through Ollama's queue took >10s).
- `POST /v1/memory/write` succeeded with `202 Accepted` but entries stayed `sync_status: pending`, `indexed_at: null` indefinitely.
- Direct `curl http://dgx:11434/api/embed` from the memory-hall container timed out at 30s even with all other models stopped — Ollama's eviction/load loop was saturated by a separate LLM client.

Meanwhile a **dedicated bge-m3 HTTP service** on the same embedder host (`:8790`, FastAPI + transformers) was consistently healthy. It does one thing — serve bge-m3 embeddings — and is not subject to Ollama's scheduler.

## Decision

**Add `HttpEmbedder` as a first-class embedder backend, selectable at runtime via `MH_EMBEDDER_KIND=http` + `MH_EMBED_BASE_URL=...`.**

The existing `OllamaEmbedder` remains the default for backward compatibility. Operators who already have a dedicated bge-m3 HTTP service (or any service with the same API shape) can opt in without touching code.

### API shape assumed by HttpEmbedder

```
POST /embed
  Request  : {"texts": [str, str, ...]}
  Response : {"model": str, "dimension": int, "count": int, "dense_vecs": [[float, ...], ...]}
```

This matches the reference dedicated bge-m3 service (and is trivial to wrap any embedding service that returns a vector list).

### Health probe separation

A secondary finding from the same incident: the health probe hardcoded `timeout=min(1.0, embed_timeout_s)`. That 1-second floor is fine for local Ollama, unreasonable for a remote HTTP service. Added `health_embed_timeout_s: float = 3.0` as a separate setting so operators can tune health-probe strictness independently from write-path timeout.

## Consequences

### Gains

- **No more LLM-queue starvation for embeddings.** An operator who points memory-hall at a dedicated embed service gets a hard isolation boundary from whatever else is hammering the LLM runner.
- **Swappable embed backends.** The protocol is documented and minimal; anyone can write a 20-line wrapper in front of bge-m3, nomic-embed, or a cloud embed API, and memory-hall consumes it unchanged.
- **Backward compatible.** Default remains `MH_EMBEDDER_KIND=ollama`; existing deployments do nothing.

### Costs

- **Two embedder codepaths** to maintain. Both are ~60 lines; drift risk is low but real. Covered by `tests/test_http_embedder.py` + `tests/test_smoke.py::test_health_uses_health_embed_timeout`.
- **Operator now has two settings to understand.** `MH_EMBEDDER_KIND` is explicit and documented in `docker-compose.yml` comments; acceptable overhead.

### Non-goals

- Not solving "multi-embedder with automatic failover". A single kind at a time; if the chosen backend is down, the embedder is down. Failover is the operator's circuit breaker concern, not the engine's.
- Not abstracting into a plugin system. Two concrete classes implementing the `Embedder` protocol is enough; adding plugin discovery is premature.

## Alternatives considered

### A. Stay on Ollama, preload bge-m3 permanently with `OLLAMA_KEEP_ALIVE=-1`

Rejected after direct test on the day of the incident: even with bge-m3 pinned, Ollama's scheduler still evicted it when LLM clients requested models whose total memory need exceeded free VRAM. The pin is advisory, not a hard reservation.

### B. Put nginx in front of Ollama to rewrite `/api/embed` → `:8790/embed`

Rejected: payload shapes differ (`{"input": ...}` vs `{"texts": [...]}`, `embeddings` vs `dense_vecs`). A translation layer in nginx is possible but ugly; a 60-line Python class is cleaner and testable.

### C. Make memory-hall embed in-process (no HTTP hop)

Rejected for now: requires shipping ~2GB of bge-m3 weights into the memory-hall image or as a sidecar. The "engine stays small" philosophy (README) argues against it. Operators who want in-process can wrap the CLI or Python entry points; the server path stays HTTP.

## Implementation summary

- `src/memory_hall/embedder/http_embedder.py` — new class, ~60 lines.
- `src/memory_hall/config.py` — add `embedder_kind`, `embed_base_url`, `embed_dim`, `health_embed_timeout_s`.
- `src/memory_hall/server/app.py` — factory branch on `embedder_kind`; health probe uses `health_embed_timeout_s`.
- `docker-compose.yml` — pass-through envs with sane defaults.
- `tests/test_http_embedder.py`, `tests/test_smoke.py` — coverage including dim mismatch, error propagation, empty input, and the new health-probe timeout behavior.

Total: +249 / -9, 7 files. 12 new/updated tests pass; existing test suite unaffected.
