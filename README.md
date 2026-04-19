# memory-hall

> Cross-session, cross-agent memory for the multi-agent era.

**Languages**:
[English](#english) · [繁體中文](#繁體中文) · [简体中文](#简体中文) · [日本語](#日本語) · [Deutsch](#deutsch) · [Français](#français) · [Italiano](#italiano) · [한국어](#한국어)

---

### English

A self-hostable memory layer for people running multiple AI agents (Claude, Codex, Gemini, local LLMs, humans, bots …). One hall, many agents — they share context, decisions, and observations across sessions. Zero LLM calls on the write path, so it stays fast and reliable where `mem0` and similar tools stall.

Contributions welcome. File an issue, open a PR, share what broke for you. This project is built in the open on purpose.

### 繁體中文

memory-hall 是一座給多 AI agent（Claude / Codex / Gemini / 本地 LLM / 人類 / 機器人）共用的記憶大廳。一座大廳，多位 agent——跨 session 分享上下文、決策、觀察。寫入不呼叫 LLM，所以在 `mem0` 這類工具會卡住的地方仍然快且穩。

歡迎一起來玩。開 issue、送 PR、回報你踩到的坑——這專案就是要公開地長出來。

### 简体中文

memory-hall 是一座给多 AI agent（Claude / Codex / Gemini / 本地 LLM / 人类 / 机器人）共用的记忆大厅。一座大厅，多位 agent——跨 session 分享上下文、决策、观察。写入不调 LLM，因此在 `mem0` 这类工具会卡住的地方仍然快且稳。

欢迎一起来玩。开 issue、提 PR、反馈你踩到的坑——这项目本来就要在公开场合长出来。

### 日本語

memory-hall は、複数の AI エージェント（Claude / Codex / Gemini / ローカル LLM / 人間 / ボットなど）が共有できるセルフホスト型メモリ層です。ひとつのホールに、たくさんのエージェント——セッションをまたいでコンテキスト・決定・観察を共有します。書き込み時に LLM を呼ばないので、`mem0` などが詰まる場面でも速く安定して動きます。

コントリビューション歓迎。Issue の起票、PR の提出、踏んだ罠の共有、大歓迎です。

### Deutsch

memory-hall ist eine selbst-hostbare Speicher­schicht für Setups, in denen mehrere KI-Agenten (Claude, Codex, Gemini, lokale LLMs, Menschen, Bots …) nebeneinander arbeiten. Eine Halle, viele Agenten — sie teilen Kontext, Entscheidungen und Beobachtungen über Sessions hinweg. Der Schreibpfad ruft kein LLM auf und bleibt deshalb dort schnell und zuverlässig, wo `mem0` & Co. hängen.

Beiträge sind willkommen. Issues, Pull Requests und Erfahrungsberichte machen das Projekt besser.

### Français

memory-hall est une couche mémoire auto-hébergeable conçue pour les setups multi-agents (Claude, Codex, Gemini, LLM locaux, humains, bots …). Un même hall, plusieurs agents — ils partagent contexte, décisions et observations à travers les sessions. Aucun appel LLM au moment de l'écriture : ça reste rapide et fiable là où `mem0` et consorts s'enlisent.

Les contributions sont les bienvenues. Ouvrez une issue, proposez une PR, partagez ce qui a cassé chez vous.

### Italiano

memory-hall è un livello di memoria self-hosted pensato per chi fa girare più agenti AI insieme (Claude, Codex, Gemini, LLM locali, persone, bot …). Una sola hall, tanti agenti — condividono contesto, decisioni e osservazioni tra una sessione e l'altra. La scrittura non chiama nessun LLM, quindi rimane veloce e affidabile anche dove `mem0` e simili si piantano.

I contributi sono benvenuti. Aprite una issue, mandate una PR, raccontate cosa si è rotto da voi.

### 한국어

memory-hall 은 여러 AI 에이전트(Claude / Codex / Gemini / 로컬 LLM / 사람 / 봇 등)가 함께 쓰는 셀프 호스트형 메모리 레이어입니다. 한 홀에 여러 에이전트가 모여 세션을 넘나들며 컨텍스트·결정·관찰을 공유합니다. 쓰기 경로에서 LLM을 호출하지 않기 때문에 `mem0` 같은 도구가 멈추는 상황에서도 빠르고 안정적으로 동작합니다.

기여는 언제든 환영합니다. 이슈 등록, PR 제출, 버그 제보 모두 환영이에요.

---

**Status:** v0.1 dogfood — API will change. Do not yet use in production.

## Why

Existing AI memory tools (mem0, Letta, …) are Claude-only or chat-only and burn LLM calls on every write — making them slow, expensive, and unreliable at scale. memory-hall is built for a different shape:

- **One memory hall, many agents.** Claude writes via MCP, Codex writes via CLI, your shell scripts write via curl. They all read the same store.
- **Cross-session by default.** Every entry is timestamped, agent-tagged, and namespaced. Pick up tomorrow where any agent left off today.
- **Zero-LLM write path.** Writes are SQLite + embedding only — P99 < 100ms on local-only deployments (Ollama same host, sqlite-vec, single tenant). Optional enrichment runs in the background, never blocks you.
- **Min local, max distributed.** Runs on a single laptop with `sqlite-vec` and local Ollama embeddings. Scales to a multi-host home AI lab with Qdrant + remote embedder via [memory-gateway](https://github.com/MakiDevelop/memory-gateway).
- **Multi-tenant data model from day one.** Schema-ready for multi-user release; v0.1 runtime ships single-tenant for focus (see [ADR 0002](docs/adr/0002-multi-tenant-from-day-one.md)).

## Quickstart (single-agent, 1 minute, no auth)

Requires: `python>=3.12`, `ollama` running locally, `bge-m3` model pulled.

```bash
ollama pull bge-m3
git clone https://github.com/MakiDevelop/memory-hall
cd memory-hall
uv sync
uv run memory-hall serve  # listens on :9000, dev mode (no auth, localhost-only)

# In another terminal:
curl -X POST http://localhost:9000/v1/memory/write \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"shell","namespace":"home","type":"note","content":"hello memory hall"}'
```

> **Auth note:** the standalone `memory-hall serve` runs in dev mode by default — no HMAC, no nonce, listens on localhost. For production / multi-user / network-exposed deployments, deploy via [`memory-gateway`](https://github.com/MakiDevelop/memory-gateway), which adds HMAC auth, ACL, and governance APIs on top of memory-hall. See [ADR 0003](docs/adr/0003-engine-library-vs-deployment-platform.md) for the engine/platform split.

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

## What this is / isn't (honest expectations)

**What v0.2 is**
- A local-first memory engine: write, search (hybrid / semantic / lexical), reindex. HTTP + CLI + in-process embedded mode.
- Runs on one box or your home lab (Mac mini + DGX Spark embedder over Tailscale in our setup).
- CJK-aware: FTS uses jieba-based pre-tokenization, so Chinese queries don't miss on substring boundaries.
- Durable by default: SQLite WAL, atomic write → index → vector, content_hash dedup, graceful degradation when the embedder is down (HTTP 202 + `sync_status=pending` + background reindex).
- Dogfooded by the seven-agent stack that built it (Claude / Codex / Gemini / Max / Grok / gemma4 / Maki).

**What v0.2 is *not*, yet**
- Not a distributed database. No replication, no consensus. One writer, one reader.
- Not production-scale for millions of entries. SQLite + sqlite-vec vec0 comfortably to ~100k on commodity hardware; beyond that, swap the vector adapter.
- No MCP server yet (planned v0.3). Today you talk HTTP or in-process import.
- No authentication. `X-Tenant-ID` header routes tenants but doesn't authenticate them. Put it behind your own gateway.
- No multi-tenant validation at scale. Schema is multi-tenant from day one (ADR-0002) but we haven't stress-tested cross-tenant isolation.
- No fancy enrichment. Writes go in roughly as given; there's no automatic fact extraction, summarization, or linking.

## Status & roadmap

- **v0.1** (2026-04-18) — engine shipped. Hit@3 hybrid=60% / lexical=60% / semantic=0% on 177-entry CJK corpus. Durability + concurrency verified. [results-2026-04-18.md](docs/benchmarks/results-2026-04-18.md).
- **v0.2** (2026-04-19, current) — jieba CJK tokenizer (pure-CJK queries now lexically hit), Gemini-round-1 nice-to-haves + v0.1 backlog cleared, bumped sqlite-vec 0.1.6 → 0.1.9 (upstream ARM64 ELF32 bug, [#251](https://github.com/asg017/sqlite-vec/issues/251)), Dockerfile build-time vec0 smoke test. [results-2026-04-19.md](docs/benchmarks/results-2026-04-19.md).
- **v0.3** — MCP server, Qdrant adapter, docker compose for self-host, auth/tenant enforcement.
- **v1.0** — public release, docs site, example integrations.
- **v2.0** — optional enrichment worker (async fact extraction), more embedder/store adapters.

See [`docs/adr/`](docs/adr/) for architectural decisions, including [why we dropped mem0](docs/adr/0001-drop-mem0.md).

## License

Apache 2.0. Contributions welcome — see `CONTRIBUTING.md` (TODO).
