# memory-hall

> **The AI agent memory engine that deliberately stays small.**
>
> SQLite + sqlite-vec + Ollama · CJK-native · HTTP / CLI / Python embedded · Apache 2.0

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![Tests](https://github.com/MakiDevelop/memory-hall/actions/workflows/ci.yml/badge.svg)](https://github.com/MakiDevelop/memory-hall/actions/workflows/ci.yml)
[![Status](https://img.shields.io/badge/status-v0.2_dogfood-orange)](#roadmap)

---

Most AI agent memory tools want to become platforms. memory-hall refuses to.

It's three components (SQLite + sqlite-vec + Ollama), three entry points (HTTP / CLI / Python embedded), and one deliberate philosophy: **the engine only stores and retrieves. Your agent stack decides the memory structure.** No opinionated enrichment, no MCP, no auth, no replica. Just a fast, durable, CJK-aware store that runs on a single Mac mini.

---

## Why memory-hall exists

AI agent memory in 2026 has no canonical implementation — OpenAI's is closed, Anthropic's is preview, and the OSS landscape split two ways:

- **Mem0 / Zep / LangMem** → SaaS or heavy. You rent memory, pay per volume, and inherit their opinions.
- **engram-rs / robotmem / MemOS** → local-first but **growing features** (decay, topic trees, spatial retrieval, OS abstraction). Great if you want structured memory, complex if you don't.

memory-hall sits in the same neighborhood as engram-rs/robotmem/MemOS but **chose the opposite direction**:

```
engram-rs, robotmem, MemOS  →  grow upward (more features, opinions, abstractions)
memory-hall                 →  shrink downward (less features, zero opinions, one engine)
```

If you want a memory engine that just stores and retrieves — and leaves structure to your agent stack — this is it.

---

## 30-second quickstart

```bash
git clone https://github.com/MakiDevelop/memory-hall
cd memory-hall && docker compose up -d
curl -X POST http://localhost:9100/v1/memory/write \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "my-agent",
    "namespace": "shared",
    "type": "episode",
    "content": "memory-hall works!"
  }'
```

Search:

```bash
curl -X POST http://localhost:9100/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"query": "memory-hall", "mode": "hybrid", "limit": 5}'
```

That's it. No auth, no account, no API key. Your data lives in `./data/memory-hall.sqlite3`.

---

## CJK-native: the killer chart

Pure-Chinese query "撞牆" (a common CJK phrase, meaning "hit a wall") against a hall containing Chinese content:

| Tokenizer | BM25 score | Verdict |
|-----------|-----------|---------|
| `unicode61` (default in most OSS) | **0** | Miss |
| `jieba` pre-tokenization (memory-hall) | **0.26** | Hit |

Why the gap? `unicode61` treats a continuous stretch of Chinese characters as **one token**, so substring queries miss. memory-hall pre-tokenizes with jieba (with bigram fallback for proper nouns and novel compounds) before indexing in FTS5, both on write and on query.

70% of my own memory content is Chinese. If yours is too, this difference matters.

---

## vs alternatives

| | memory-hall | mem0 | engram-rs | robotmem | MemOS |
|---|---|---|---|---|---|
| Deployment | self-host | SaaS | self-host | self-host | self-host + cloud |
| Language | Python | Python | Rust | Python | Python |
| Storage | SQLite + sqlite-vec | Qdrant/pgvector | SQLite + FTS5 | SQLite + FTS5 + vec0 | multi-store |
| CJK first-class | **✅ jieba at storage layer** | ❌ (via embedder) | ✅ (BM25 + jieba) | ✅ (jieba) | ❌ (via embedder) |
| MCP server | ❌ by choice | — | ❌ | ✅ | — |
| Enrichment / decay | ❌ by choice | ✅ | ✅ 3-layer decay | — | ✅ scheduler |
| Authentication | ❌ by choice | ✅ | — | — | ✅ |
| Deliberate scope ceiling | **✅ engine only** | ❌ growing | — | — | ❌ "OS for memory" |
| License | Apache 2.0 | mixed | Apache 2.0 | — | Apache 2.0 |

Not "mine is best". memory-hall's bet is that **engine ≠ platform**. If you want an opinionated memory product, use mem0 or MemOS. If you want a minimal engine you can compose your own structure around, use this.

---

## Designed to NOT do (the actual feature)

Most READMEs list what a project does. This is the list of what memory-hall **deliberately doesn't** — each one a design choice, not a TODO:

| Feature | Why not | When it'd change |
|---------|---------|------------------|
| **MCP server** | Adds setup friction; protocol still evolving | v0.3, when use cases crystallize |
| **Authentication** | Gets in the way of personal/home lab; bad early picks are hard to undo | When someone deploys this behind an exposed endpoint |
| **Replica / HA** | SQLite's whole value is single-file simplicity; adding consensus violates that | At v2.0, via Postgres adapter swap |
| **Enrichment worker** (fact extraction, summarization) | Opinionated memory structure is what makes mem0 not fit my use case; I won't repeat that | Never in this repo. Build it on top. |
| **Memory decay / topic tree** | Same as above — memory shape is your agent stack's job | Never in this repo. |
| **Knowledge graph** | Same | Never in this repo. |

memory-hall's core promise is three steps:

```
docker compose up  →  curl POST /v1/memory/write  →  you have memory
```

Every feature above would break that promise. The promise is the product.

See [`docs/adr/0003-engine-library-vs-deployment-platform.md`](docs/adr/0003-engine-library-vs-deployment-platform.md) for the full engine-vs-platform rationale.

---

## Three entry points

| Entry | Audience | Status |
|---|---|---|
| HTTP REST `:9100/v1/*` | any language, any tool | v0.2 |
| CLI `mh` | Codex, Gemini CLI, shell scripts | v0.2 |
| Python embedded (in-process) | sandboxed agents, tests, batch imports | v0.2 |

No entry is privileged — they all hit the same backend, so no single-point-of-failure path.

> **Agents reading this**: see [`docs/agent-integration.md`](docs/agent-integration.md) for a decision tree that picks the right surface based on your sandbox, plus the auth + install gotchas that have bitten real Codex / Gemini sessions.

### Embedded (in-process) use

Some agents run in sandboxes that block localhost sockets (Codex CLI, some Gemini setups, restricted containers). For those, skip HTTP entirely:

```python
import asyncio
from memory_hall import Settings, build_runtime
from memory_hall.models import WriteMemoryRequest, SearchMemoryRequest

async def main():
    runtime = build_runtime(settings=Settings())
    await runtime.start()
    try:
        await runtime.write_entry(
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
        print(hits.total)
    finally:
        await runtime.stop()

asyncio.run(main())
```

No network, no auth, same storage.

---

## Multi-host deployment

Run the engine on one machine, put the embedder on another. My home lab:

```
[Mac mini M4] memory-hall:0.2.0  ─── Tailscale ──→  [DGX Spark 128GB] Ollama bge-m3
     │
     └── rsync /5min ──→  [Mac mini #2] memory-hall:0.2.0 (cold standby)
```

Primary dies → manually `docker start memory-hall` on standby. Not true HA, but 80% of real HA value for personal / home setups with zero maintenance overhead.

### When your Ollama is also serving LLMs: use `HttpEmbedder`

If the Ollama instance that memory-hall embeds against is *also* serving LLM traffic (e.g. you share one DGX Spark Ollama between an agent chat stack and memory-hall), `bge-m3` can get starved by the LLM queue and `/v1/health` will flap between `ok` and `degraded`. Hit that once (hard) in production — see [`docs/operations/incident-2026-04-20-embed-queue.md`](docs/operations/incident-2026-04-20-embed-queue.md).

Workaround since 0.2.1: point memory-hall at a **dedicated embed service** (any service speaking `POST /embed {"texts": [...]}` → `{"dense_vecs": [...]}`) via:

```yaml
environment:
  MH_EMBEDDER_KIND: http
  MH_EMBED_BASE_URL: http://<embed-host>:8790
```

Rationale in [ADR 0006](docs/adr/0006-http-embedder-embed-queue-isolation.md). The default `MH_EMBEDDER_KIND=ollama` is unchanged — existing deployments do nothing.

### Opt-in token auth

Set `MH_API_TOKEN` to require `Authorization: Bearer <token>` on all `/v1/memory/*` endpoints (`/v1/health` stays public). Leave unset for dev. Rationale + when to upgrade to HMAC in [ADR 0007](docs/adr/0007-minimal-token-auth.md).

---

## What v0.2 is / isn't (honest expectations)

**What v0.2 is**
- Write, search (hybrid / semantic / lexical), reindex. HTTP + CLI + Python embedded.
- CJK-aware via jieba at storage layer.
- Durable by default: SQLite WAL, atomic write → index → vector, `content_hash` dedup, graceful degradation (HTTP 202 + `sync_status=pending` + background reindex worker).
- Battle-tested under 50-way concurrent writes (zero data loss) and embedder outages (writes keep succeeding).
- Dogfooded by seven AI agents (Claude / Codex / Gemini / Max / Grok / gemma4 / the human) during development.

**What v0.2 is *not*, yet**
- Not a distributed database. One writer, one reader.
- Not production-scale for millions of entries. sqlite-vec is comfortable to ~100k on commodity hardware; beyond that, swap the vector adapter.
- No MCP server yet (v0.3).
- No authentication (put it behind your own gateway).
- No multi-tenant validation at scale (schema is multi-tenant from day one per [ADR-0002](docs/adr/0002-multi-tenant-from-day-one.md), but cross-tenant isolation at scale isn't stress-tested).
- No enrichment. What you write is what gets stored.

---

## Status & roadmap

- **v0.1** (2026-04-18) — engine shipped. Hit@3 hybrid=60% / lexical=60% / semantic=0% on 177-entry CJK corpus. Durability + concurrency verified. See [results-2026-04-18.md](docs/benchmarks/results-2026-04-18.md).
- **v0.2** (2026-04-19) — jieba CJK tokenizer (pure-CJK queries now lexically hit: BM25 0 → 0.26), latency metrics in benchmark, cursor-stream reindex, `embed_batch` for backlog throughput, Docker sqlite-vec upgraded to 0.1.9 (upstream [#251](https://github.com/asg017/sqlite-vec/issues/251) ARM64 ELF32 bug), build-time `vec0` smoke test. See [results-2026-04-19.md](docs/benchmarks/results-2026-04-19.md).
- **v0.2.1** (2026-04-20, **current**) — `HttpEmbedder` backend (ADR 0006) for isolating the embed path from shared-Ollama LLM queues; `health_embed_timeout_s` separated from write-path timeout; `docker-compose.yml` default host port corrected to 9100. See [CHANGELOG](CHANGELOG.md).
- **v0.3** — MCP server, Qdrant adapter, docker compose for self-host, optional auth. (Let the use cases find us first.)
- **v1.0** — public release, docs site, example integrations.
- **v2.0** — Postgres adapter for replica/HA, more embedder/store adapters.

Architecture decisions: [`docs/adr/`](docs/adr/), including [why we dropped mem0](docs/adr/0001-drop-mem0.md).

---

## FAQ

**Why not just use mem0?**
mem0's trade-offs (SaaS-first, English-leaning tokenization, opinionated enrichment, version evolution you can't pin) don't fit if you're running multiple agents locally with CJK-heavy content. See the [full 6-reason breakdown](https://blog.chibakuma.com/memory-hall-intro).

**Why not use engram-rs or robotmem?**
They're excellent and doing different things — engram-rs adds temporal memory decay and topic trees (Rust), robotmem adds MCP + spatial retrieval (Python, AI robots). memory-hall deliberately skips those layers. If you want opinionated memory structure, pick them. If you want minimal raw engine, pick this. They're not competitors, they're neighbors.

**Why jieba specifically?**
It's the established CJK segmentation library (≥14 years old, pure Python, no native deps), works well enough for 95% of Chinese content, and fails gracefully (bigram fallback for proper nouns). The `jieba` decision can be swapped later if a better option appears — but that's not today's problem.

**Why SQLite?**
Single-file deployment, no server to run, WAL for concurrent reads, 64-bit ARM wheels for `sqlite-vec` make it actually fast on Apple Silicon, and `mv memory-hall.sqlite3 /new/path` is your migration plan. The moment you need replica/HA, swap to Postgres — but most personal / home / small-team uses never cross that line.

**Can I use this in production?**
For internal/personal deployments, sure — I dogfood it on my home AI lab and it's shipped multiple v0.x releases without data loss. For a product you're selling, wait for v1.0 or a security review. API stability: v0.x can break; v1.x stable.

**How do I contribute?**
Open an issue (bug reports from real usage are the most valuable — see [Max's 30/60/90 day rubric](docs/council/2026-04-19-memory-hall-worth-it-codex-answer.md) if curious), submit a PR, share what broke for you. The project is built in the open on purpose.

---

## License

[Apache 2.0](LICENSE).

---

## Intros in other languages

<details>
<summary>繁體中文</summary>

memory-hall 是給多 AI agent（Claude / Codex / Gemini / 本地 LLM / 人類 / 機器人）共用的本地記憶引擎。用 SQLite + sqlite-vec + Ollama 一台 Mac mini 就能跑，CJK 原生（jieba 預切詞），Apache 2.0。

**故意保持小**——沒有 decay、沒有 topic tree、沒有 MCP、沒有 auth、沒有 enrichment worker。agent memory 最容易 bloat 成「另一個平台」，memory-hall 的賭注是：engine 只管儲存，agent stack 主人決定記憶結構。

歡迎一起來玩。開 issue、送 PR、回報你踩到的坑。完整論據見 [blog](https://blog.chibakuma.com/memory-hall-cjk-first-mover)。

</details>

<details>
<summary>简体中文</summary>

memory-hall 是给多 AI agent（Claude / Codex / Gemini / 本地 LLM / 人类 / 机器人）共用的本地记忆引擎。用 SQLite + sqlite-vec + Ollama 一台 Mac mini 就能跑，CJK 原生（jieba 预切词），Apache 2.0。

**故意保持小**——没有 decay、没有 topic tree、没有 MCP、没有 auth、没有 enrichment worker。memory-hall 的赌注是：engine 只管存储，agent stack 主人决定记忆结构。

</details>

<details>
<summary>日本語</summary>

memory-hall は、複数の AI エージェント（Claude / Codex / Gemini / ローカル LLM / 人間 / ボット）が共有できるセルフホスト型メモリエンジンです。SQLite + sqlite-vec + Ollama で Mac mini 一台で動きます。CJK ネイティブ（jieba 分かち書き）、Apache 2.0。

**意図的に小さく保つ**——decay なし、topic tree なし、MCP なし、auth なし、enrichment worker なし。memory-hall の賭けは：エンジンは保存と検索だけ、メモリ構造の決定はエージェントスタックの持ち主に任せる。

</details>

<details>
<summary>English</summary>

memory-hall is a self-hostable memory engine for multiple AI agents (Claude, Codex, Gemini, local LLMs, humans, bots). SQLite + sqlite-vec + Ollama runs on a single Mac mini. CJK-native via jieba tokenization. Apache 2.0.

**Deliberately small** — no decay, no topic tree, no MCP, no auth, no enrichment worker. memory-hall's bet: the engine only stores and retrieves; memory structure is your agent stack's decision.

</details>

<details>
<summary>Deutsch</summary>

memory-hall ist eine selbst-hostbare Memory-Engine für mehrere KI-Agenten (Claude, Codex, Gemini, lokale LLMs, Menschen, Bots). SQLite + sqlite-vec + Ollama — läuft auf einem Mac mini. CJK-nativ via jieba-Tokenisierung. Apache 2.0.

**Absichtlich klein gehalten** — kein Decay, kein Topic Tree, kein MCP, keine Auth, kein Enrichment-Worker. Die Engine speichert und ruft ab; die Memory-Struktur entscheidet dein Agent-Stack.

</details>

<details>
<summary>Français</summary>

memory-hall est un moteur mémoire auto-hébergeable pour plusieurs agents IA (Claude, Codex, Gemini, LLM locaux, humains, bots). SQLite + sqlite-vec + Ollama tournent sur un seul Mac mini. CJK natif via tokenisation jieba. Apache 2.0.

**Volontairement petit** — pas de decay, pas de topic tree, pas de MCP, pas d'auth, pas de worker d'enrichissement. Le moteur stocke et récupère ; la structure de la mémoire, c'est à votre agent stack de la décider.

</details>

<details>
<summary>Italiano</summary>

memory-hall è un motore di memoria self-hosted per più agenti AI (Claude, Codex, Gemini, LLM locali, umani, bot). SQLite + sqlite-vec + Ollama girano su un singolo Mac mini. CJK nativo tramite tokenizzazione jieba. Apache 2.0.

**Volutamente piccolo** — niente decay, niente topic tree, niente MCP, niente auth, niente enrichment worker. Il motore salva e recupera; la struttura della memoria la decide il tuo agent stack.

</details>

<details>
<summary>한국어</summary>

memory-hall 은 여러 AI 에이전트(Claude / Codex / Gemini / 로컬 LLM / 사람 / 봇)가 함께 쓰는 셀프 호스트형 메모리 엔진입니다. SQLite + sqlite-vec + Ollama 로 Mac mini 한 대에서 돌아갑니다. CJK 네이티브(jieba 토큰화), Apache 2.0.

**의도적으로 작게 유지** — decay 없음, topic tree 없음, MCP 없음, auth 없음, enrichment worker 없음. memory-hall의 베팅: 엔진은 저장과 검색만, 메모리 구조 결정은 당신의 에이전트 스택이.

</details>
