# memory-hall

> Cross-session, cross-agent memory for the multi-agent era.

A self-hostable memory layer for people running multiple AI agents (Claude, Codex, Gemini, local LLMs, …) and wanting them to share context, decisions, and observations across sessions.

**Status:** v0.1 dogfood — API will change. Do not yet use in production.

## Why

Existing AI memory tools (mem0, Letta, …) are Claude-only or chat-only and burn LLM calls on every write — making them slow, expensive, and unreliable at scale. memory-hall is built for a different shape:

- **One memory hall, many agents.** Claude writes via MCP, Codex writes via CLI, your shell scripts write via curl. They all read the same store.
- **Cross-session by default.** Every entry is timestamped, agent-tagged, and namespaced. Pick up tomorrow where any agent left off today.
- **Zero-LLM write path.** Writes are SQLite + embedding only — P99 < 100ms. Optional enrichment runs in the background, never blocks you.
- **Min local, max distributed.** Runs on a single laptop with `sqlite-vec` and local Ollama embeddings. Scales to a multi-host home AI lab with Qdrant + remote embedder.
- **Multi-tenant from day one.** Built so you can release access to others — friends, teammates, your own bots — with strict namespace isolation.

## Quickstart (single-agent, 1 minute)

Requires: `python>=3.12`, `ollama` running locally, `bge-m3` model pulled.

```bash
ollama pull bge-m3
git clone https://github.com/USERNAME/memory-hall
cd memory-hall
uv sync
uv run memory-hall serve  # listens on :9000

# In another terminal:
curl -X POST http://localhost:9000/v1/memory/write \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"shell","namespace":"home","type":"note","content":"hello memory hall"}'
```

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

## Status & roadmap

- **v0.1** (current) — single-tenant runtime, SQLite + Ollama, HTTP + CLI
- **v0.2** — MCP server, Qdrant adapter, multi-tenant validation, docker compose for self-host
- **v1.0** — public release, docs site, example integrations
- **v2.0** — optional enrichment worker (async fact extraction), more embedder/store adapters

See [`docs/adr/`](docs/adr/) for architectural decisions, including [why we dropped mem0](docs/adr/0001-drop-mem0.md).

## License

Apache 2.0. Contributions welcome — see `CONTRIBUTING.md` (TODO).
