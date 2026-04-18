# memory-hall

> Cross-session, cross-agent memory for the multi-agent era.

A self-hostable memory layer for people running multiple AI agents (Claude, Codex, Gemini, local LLMs, …) and wanting them to share context, decisions, and observations across sessions.

**Status:** v0.1 dogfood — API will change. Do not yet use in production.

## Why

Existing AI memory tools (mem0, Letta, …) are Claude-only or chat-only and burn LLM calls on every write — making them slow, expensive, and unreliable at scale. memory-hall is built for a different shape:

- **One memory hall, many agents.** Claude writes via MCP, Codex writes via CLI, your shell scripts write via curl. They all read the same store.
- **Cross-session by default.** Every entry is timestamped, agent-tagged, and namespaced. Pick up tomorrow where any agent left off today.
- **Zero-LLM write path.** Writes are SQLite + embedding only — P99 < 100ms on local-only deployments (Ollama same host, sqlite-vec, single tenant). Optional enrichment runs in the background, never blocks you.
- **Min local, max distributed.** Runs on a single laptop with `sqlite-vec` and local Ollama embeddings. Scales to a multi-host home AI lab with Qdrant + remote embedder via [memory-gateway](https://github.com/USERNAME/memory-gateway).
- **Multi-tenant data model from day one.** Schema-ready for multi-user release; v0.1 runtime ships single-tenant for focus (see [ADR 0002](docs/adr/0002-multi-tenant-from-day-one.md)).

## Quickstart (single-agent, 1 minute, no auth)

Requires: `python>=3.12`, `ollama` running locally, `bge-m3` model pulled.

```bash
ollama pull bge-m3
git clone https://github.com/USERNAME/memory-hall
cd memory-hall
uv sync
uv run memory-hall serve  # listens on :9000, dev mode (no auth, localhost-only)

# In another terminal:
curl -X POST http://localhost:9000/v1/memory/write \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"shell","namespace":"home","type":"note","content":"hello memory hall"}'
```

> **Auth note:** the standalone `memory-hall serve` runs in dev mode by default — no HMAC, no nonce, listens on localhost. For production / multi-user / network-exposed deployments, deploy via [`memory-gateway`](https://github.com/USERNAME/memory-gateway), which adds HMAC auth, ACL, and governance APIs on top of memory-hall. See [ADR 0003](docs/adr/0003-engine-library-vs-deployment-platform.md) for the engine/platform split.

## Quickstart (multi-host, your home AI lab)

```bash
docker compose up -d  # starts memory-hall + qdrant
# point your embedder host (e.g. DGX Spark) via OLLAMA_BASE env
```

See [`docs/design.md`](docs/design.md) for full architecture and [`docs/api.md`](docs/api.md) for the API reference.

## Three entry points

| Entry | Audience | Status |
|---|---|---|
| HTTP REST `:9000/v1/*` | any language, any tool | v0.1 |
| MCP server | Claude Code, Claude Desktop | planned v0.2 |
| CLI `mh` | Codex, Gemini CLI, shell scripts | v0.1 |

No entry is privileged — they all hit the same backend, so no single-point-of-failure path.

### Embedded (in-process) use

Some agents run in sandboxes that block localhost sockets (Codex CLI, some Gemini setups, restricted containers). For those, skip HTTP entirely and use memory-hall as a Python library:

```python
import asyncio
from memory_hall import Settings, build_runtime
from memory_hall.models import WriteMemoryRequest, SearchMemoryRequest

async def main():
    runtime = build_runtime(settings=Settings())
    await runtime.start()
    try:
        written = await runtime.write_entry(
            tenant_id="default",
            principal_id="my-agent",
            payload=WriteMemoryRequest(
                agent_id="my-agent",
                namespace="shared",
                type="note",
                content="hello from inside the process",
            ),
        )
        hits = await runtime.search_entries(
            tenant_id="default",
            payload=SearchMemoryRequest(query="hello", limit=5),
        )
        print(written.entry.entry_id, hits.total)
    finally:
        await runtime.stop()

asyncio.run(main())
```

No network, no auth, same storage. Useful for sandboxed agents, tests, batch imports.

## Status & roadmap

- **v0.1** (current) — single-tenant runtime, SQLite + Ollama, HTTP + CLI
- **v0.2** — MCP server, Qdrant adapter, multi-tenant validation, docker compose for self-host
- **v1.0** — public release, docs site, example integrations
- **v2.0** — optional enrichment worker (async fact extraction), more embedder/store adapters

See [`docs/adr/`](docs/adr/) for architectural decisions, including [why we dropped mem0](docs/adr/0001-drop-mem0.md).

## License

Apache 2.0. Contributions welcome — see `CONTRIBUTING.md` (TODO).
