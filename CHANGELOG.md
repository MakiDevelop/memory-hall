# Changelog

All notable changes to memory-hall are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). memory-hall uses versioned 0.x releases; see [ADR 0005](docs/adr/0005-v0.2-minimum-viable-contract.md) for what's frozen vs free-to-change at each 0.x version.

## [Unreleased]

### Added

- **`MH_API_TOKEN` minimal-token auth** (opt-in). When set, every endpoint except `GET /v1/health` requires `Authorization: Bearer <token>`. Unset = no auth (backward compatible). Rationale, scope limits, and sunset criteria in [ADR 0007](docs/adr/0007-minimal-token-auth.md). Generate a token with `openssl rand -hex 32`.
- **`HttpEmbedder`** — second embedder backend alongside `OllamaEmbedder`. Speaks a minimal `POST /embed` / `{"texts": [...]}` → `{"dense_vecs": [...]}` contract. Opt in with `MH_EMBEDDER_KIND=http` + `MH_EMBED_BASE_URL=...`. Rationale in [ADR 0006](docs/adr/0006-http-embedder-embed-queue-isolation.md).
- **`health_embed_timeout_s`** config (default `3.0s`) — separate knob for `/v1/health` embed-probe timeout, independent from write-path `embed_timeout_s`. Fixes a 1-second hardcoded timeout that was too tight for remote embed services.
- **Operator footgun docs** in [`docs/deploy.md`](docs/deploy.md) and a full incident writeup at [`docs/operations/incident-2026-04-20-embed-queue.md`](docs/operations/incident-2026-04-20-embed-queue.md). Covers Ollama-queue starvation, named-volume replacement during compose recreate, and macOS keychain non-interactive builds.

### Changed

- **`docker-compose.yml`** data volume is now a bind mount driven by `MEMHALL_DATA_DIR` (default `./mh-data` inside the repo). Previously a named `mh-data:` volume, which was the root of the 2026-04-20 Issue 2 near-miss. Existing deployments on a named volume: see [`docs/deploy.md`](docs/deploy.md) for the migration steps.
- **`docker-compose.yml`** host port is now `9100:9000` (was `9000:9000`). Matches what existing operator docs and examples already assumed. If you pinned the old `9000` port in downstream callers, update before recreating the container.
- **Health probe** (`_refresh_health_cache`) uses `health_embed_timeout_s` instead of `min(1.0, embed_timeout_s)`. This is a behavioral tightening of the v0.2 `/v1/health` contract only for the `degraded` threshold — the response shape is unchanged.

### Fixed

- Health probe no longer false-degrades when the embedder is a remote HTTP service with typical (~500ms–1s) cold-path latency.

## 0.2.0 — 2026-04-19

v0.2 minimum viable contract freeze. See [ADR 0005](docs/adr/0005-v0.2-minimum-viable-contract.md) for the full frozen surface.

Highlights:
- `/v1/memory/write`, `/v1/memory/search`, `/v1/health` contracts declared stable for the v0.2.x line.
- sqlite-vec v0.1.6 as the default vector store.
- Content-hash–based deduplication (`(agent_id, namespace, type, content)` → deterministic `entry_id`).
- Multi-tenant data model (single-tenant runtime in 0.2, multi-tenant deferred to 0.3+).

## 0.1.0 — 2026-04-18

Initial public release. See [ADR 0001](docs/adr/0001-drop-mem0.md) for the project's founding rationale (why memory-hall exists vs mem0 / LangMem / Zep).

Core:
- SQLite + sqlite-vec storage.
- Ollama (`bge-m3`) embeddings.
- HTTP API (`/v1/memory/write`, `/v1/memory/search`, `/v1/memory/{id}`, `/v1/health`).
- CLI (`memory-hall write / search / list / reindex-fts`).
- Python embedded usage (`from memory_hall import Settings, build_runtime`).
