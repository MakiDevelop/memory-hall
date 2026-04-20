# Incident 2026-04-20 — Embed-path starvation + volume replacement during deploy

**Severity**: High (memhall write path degraded for ~6h; data loss narrowly avoided via backup)
**Duration**: ~6 hours of `embedder: degraded` / `sync_status: pending` before detection
**Resolved**: 2026-04-20 18:25 Taipei

Two cascading issues hit memhall's primary deployment (single Mac mini) on the same day. Both are operator-facing, not engine bugs. Documented here so others deploying memory-hall in similar topologies can avoid them.

---

## Issue 1 — Ollama LLM queue starves bge-m3

### Symptom

- `/v1/health` reported `embedder: degraded` for hours without recovery.
- `POST /v1/memory/write` returned 202 but entries persisted with `sync_status: pending`, `indexed_at: null`.
- Reading worked (lexical/FTS fallback), but semantic search scores collapsed because new writes weren't in the vector index.

### Root cause

memory-hall pointed its embedder at a shared Ollama instance (`MH_OLLAMA_BASE_URL=...:11434`). That Ollama was simultaneously serving large LLM clients (qwen3-vl, qwen3.5:35b, etc.) whose combined model weights exceeded available GPU memory. Ollama's scheduler entered a constant evict/load loop. `bge-m3` (small, fast) could not win a slot: every embed request saw a cold load that timed out before bge-m3 got loaded.

Direct test on the day:
- `curl .../api/tags` — 200 in <1s (Ollama metadata is fine).
- `curl .../api/embed -d '{"model":"bge-m3", ...}'` — 30s timeout, no response, even after `ollama stop` on the blocking LLM.

A dedicated bge-m3 HTTP service on the same host (`:8790`, just FastAPI + transformers) was consistently healthy throughout.

### Resolution

Introduced `HttpEmbedder` (see [ADR 0006](../adr/0006-http-embedder-embed-queue-isolation.md)). Set:

```
MH_EMBEDDER_KIND=http
MH_EMBED_BASE_URL=http://<dedicated-embed-host>:8790
```

After redeploy: `/v1/health` immediately returned `ok`; new writes completed with `embedded: true` synchronously; semantic search scores recovered (RRF 0.033 / semantic 0.638 on the canonical test query, vs 0.016 / unavailable while degraded).

### Operator guidance

If you share one Ollama instance across multiple agent stacks, **do not use it for embeddings**. Ollama's scheduler is not designed for mixed small-frequent (embed) + large-rare (LLM) workloads. Either:

1. Run a dedicated embed service (any service with `POST /embed {"texts":[...]}` → `{"dense_vecs":[...]}` shape works with `HttpEmbedder`), or
2. Dedicate an Ollama instance exclusively to embedding models (no LLM clients allowed).

---

## Issue 2 — Named-volume replaced when switching from `docker run` to `docker compose`

### Symptom

During the fix for Issue 1, redeploying via `docker compose up -d --force-recreate memory-hall` silently created a new empty `memory-hall_mh-data` named volume. The running container came up healthy but with **zero existing entries** visible.

### Root cause

The original deployment used `docker run -v memory-hall_mh-data:/data ...` (or a similarly-named volume), created ad-hoc. When `docker-compose.yml` declared a volume of the same short name (`mh-data`), compose treats the project namespace: the effective volume becomes `${project}_mh-data` = `memory-hall_mh-data` — but **only when Compose manages it**. An existing volume of the same literal name, created outside Compose, does not automatically inherit Compose project labels.

What actually happened in this deploy (reconstructed from `docker volume inspect` timestamps): the pre-existing volume was treated as an orphan by Compose and replaced with a freshly-created empty volume carrying the correct `com.docker.compose.project` labels. The old volume's data was not mounted into the new container.

Data was recovered from a JSONL dump that happened to be taken for unrelated reasons ~9 hours earlier. Without that dump, the 47 pre-existing entries would have been lost.

### Operator guidance (critical)

Before running `docker compose up --force-recreate` against a service that was previously started via plain `docker run`:

1. **Back up the data directory first.** For memhall:
   ```bash
   docker run --rm -v memory-hall_mh-data:/backup alpine \
       tar czf - /backup > memhall-backup-$(date +%F).tar.gz
   ```
   Or use the bind-mount layout recommended in [`docs/deploy.md`](../deploy.md) and snapshot the host path directly.

2. **Confirm which volume Compose will use.** `docker compose config` prints the resolved volume references. If Compose would create `${project}_<name>` but your old data is under just `<name>` (or a different path), you must either rename the old volume to match Compose's expected name, or reshape the compose file to point at the existing one explicitly.

3. **Prefer bind mounts over named volumes** for primary production data (the pattern `docs/deploy.md` already recommends). Bind mounts are transparent: the data is at a host path you control, backup is `rsync`, and Compose can't silently swap it.

4. **Keep a daily dump**, not just for disasters. A scheduled `GET /v1/memory?limit=1000&cursor=...` (or a CLI export) writing JSONL to a separate host or NAS is cheap insurance. We'll add a reference script under `deploy/` in a follow-up.

---

## Timeline

| Time (Taipei) | Event |
|---------------|-------|
| ~10:15 | memhall container started (original deployment; stayed Up 7h until intervention) |
| 17:00 | User noticed `/v1/health` returned `degraded`; investigation started |
| 17:10 | Root-caused to Ollama queue starvation; dedicated `:8790` bge-m3 service confirmed healthy |
| 17:30 | `HttpEmbedder` class + config + tests implemented |
| 17:55 | Deploy attempted via SSH; blocked by macOS keychain non-interactive limitation |
| 18:00 | Deploy script re-run on mini's local Terminal after `security -v unlock-keychain` |
| 18:10 | Port 6333 / 9100 conflicts resolved (`--no-deps`, compose port alignment 9000→9100) |
| 18:15 | New container up and healthy — but pre-existing 47 entries missing from the new volume |
| 18:20 | Restored from JSONL dump taken earlier in the day for unrelated reasons |
| 18:25 | Full recovery: 49 entries visible, embedder=ok, semantic search scores recovered |

---

## Action items

- [x] Land `HttpEmbedder` + `health_embed_timeout_s` — [ADR 0006](../adr/0006-http-embedder-embed-queue-isolation.md), merged.
- [x] Document both issues in operator-facing docs — this file + `docs/deploy.md` footgun section.
- [ ] Ship `deploy/memhall-dump.sh` — nightly JSONL dump to a separate host. Tracked in follow-up.
- [ ] Align `docker-compose.yml` default volume strategy with `docs/deploy.md` (bind mount). Currently compose uses named volume; deploy.md recommends bind mount. That gap is the core of Issue 2.
