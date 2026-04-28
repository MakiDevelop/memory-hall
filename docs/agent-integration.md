# Agent Integration Guide

If you are an AI agent (Claude / Codex / Gemini / a sub-agent / a script in a sandbox) and you need to read or write memhall, this is the doc for you.

The README's "Three entry points" lists the surfaces. This doc is the **decision tree**: which surface you should actually pick, and the gotchas each one has.

> **Status legend** (last verified 2026-04-28 against `fix/reliability-phase-a5-2026-04-27`):
> - ✅ **verified** — exercised end-to-end in a real session, including against a server with `MH_API_TOKEN` set.
> - ⚠️ **partial** — works for the no-auth case, but does **not** currently work against a server that requires `MH_API_TOKEN`.
>
> If a path is marked ⚠️ and you need it to work with auth, fall back to a ✅ path until the gap is closed.

---

## Decision tree

```
Are you running in the same process / repo as memory-hall, with `import memory_hall` available?
├─ Yes → use the embedded Python runtime  (Path A)
└─ No
   │
   Can your sandbox open a TCP socket to the memhall host?
   ├─ Yes → use HTTP + Bearer  (Path B)
   └─ No (sandboxed agents: Codex CLI, restricted containers, some Gemini setups)
       └─ install the package and use Path A in-process,
          or shell out via `mh` CLI which goes through Path A under the hood  (Path C)
```

If you do not know which one applies to you, default to **Path B (HTTP + Bearer)** — it works from anywhere that has network access and `curl`.

---

## Path A — Embedded Python (in-process) ✅

Status: ✅ verified. Bypasses HTTP + auth entirely (in-process call, no middleware).

Use when: same process, sandboxed environments where TCP is blocked, batch imports, tests.

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

**No network, no auth, same storage.** This is the path Codex / Gemini sandboxes should prefer when localhost TCP is blocked by the sandbox.

Gotchas:
- `Settings()` reads from env (`MH_DB_PATH`, `MH_EMBEDDER_KIND`, …). If the agent's working directory has its own `.env`, runtime config will diverge from the running HTTP server. Point both at the same DB if you want them to share state.
- `build_runtime` is async; you need an event loop. In a sync script, wrap with `asyncio.run(...)`.

---

## Path B — HTTP + Bearer ✅

Status: ✅ verified against a server with `MH_API_TOKEN` set. This is the most reliable path when the sandbox has TCP access.

Use when: any language, any tool, sandbox can reach the host over TCP.

```bash
# Set once per shell. Maki's setup keeps the token at ~/.config/memhall/token (0600).
export MH_API_TOKEN="$(cat ~/.config/memhall/token)"

curl -sS http://127.0.0.1:9000/v1/memory/write \
  -H "Authorization: Bearer ${MH_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "my-agent",
    "namespace": "shared",
    "type": "note",
    "content": "hello from curl"
  }'
```

Gotchas:
- **`Authorization: Bearer …` is required** on every `/v1/memory/*` request when the server has `MH_API_TOKEN` set. `/v1/health` is the only public endpoint. Missing the header returns `{"detail":"missing bearer token"}` — the server is alive, you are just unauthenticated.
- If the server runs without `MH_API_TOKEN` set (dev / standalone), the header is ignored. Sending it anyway is safe and forward-compatible — always send it.
- Default port is `9000`. Maki's home deployment maps it to `9100` (`http://100.122.171.74:9100`). Check the deployment you are talking to.
- `/v1/admin/*` requires `MH_ADMIN_TOKEN` (a different token). Regular `MH_API_TOKEN` is **rejected** on admin paths when admin token is set. See `docs/adr/0007-minimal-token-auth.md`.
- See `examples/shell/write_memory.sh` for a runnable starter.

---

## Path C — `mh` CLI ✅

Status: ✅ verified. The CLI reads `MH_API_TOKEN` from the environment (via `Settings()`) and attaches `Authorization: Bearer <token>` automatically when set. Works against both auth-enabled and no-auth servers. Verified against `src/memory_hall/cli/main.py:31` on `fix/reliability-phase-a5-2026-04-27`; covered by `tests/test_cli_auth.py`.

Use when: you want a one-liner from a shell, you do not want to hand-roll JSON, and the package is installed.

```bash
# One-time install in the project venv:
uv sync
# Then `mh` is on PATH inside the venv.

# If the server has MH_API_TOKEN set, export it (CLI reads it automatically):
export MH_API_TOKEN="$(cat ~/.config/memhall/token)"

uv run mh write "DEC-018 落地完成" \
  --agent-id codex \
  --namespace project:memory-hall \
  --type decision \
  --tag governance

uv run mh search "DEC-018"
```

Gotchas:
- `mh` is a console script defined in `pyproject.toml`. It is **not** globally available. If `command -v mh` returns nothing, you have not installed the package — run `uv sync` (or `pip install -e .`) inside the repo first.
- `uv run mh …` works without prior install but resolves dependencies on first use. In sandboxes where `~/.cache/uv` is not writable, set `UV_CACHE_DIR=/tmp/uv-cache` before calling.
- The CLI hits HTTP under the hood. `MH_API_TOKEN` is read from the environment on each command; no CLI flag is needed. If unset, no `Authorization` header is sent (works against no-auth servers).

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `{"detail":"missing bearer token"}` | Path B without `Authorization` header | Set `MH_API_TOKEN` and add `-H "Authorization: Bearer ${MH_API_TOKEN}"` |
| `curl: (7) Couldn't connect to server` from a sandboxed agent | Sandbox blocks localhost TCP | Switch to Path A (embedded Python) |
| `command not found: mh` | Package not installed in this shell's PATH | `uv sync` inside the repo, or use `uv run mh …` |
| `uv run mh` errors on `~/.cache/uv` permission | Sandbox cache dir not writable | `export UV_CACHE_DIR=/tmp/uv-cache` |
| Writes succeed but search returns nothing | Path A and Path B pointing at different DB files | Align `MH_DB_PATH` in both, or always go through HTTP |

---

## Picking the right `agent_id` and `namespace`

- `agent_id` — stable identity for the agent. Examples: `claude`, `codex`, `gemini`, `max`, `grok`, `gemma4`, `maki`. Do not invent a new id per session; one id per agent persona.
- `namespace` — scope of the entry. Examples: `home`, `work`, `project:<name>`, `agent:<id>`, `shared`.
- `type` — one of `episode`, `decision`, `observation`, `experiment`, `fact`, `note`, `question`, `answer`.

Do not write company-sensitive content into `shared` or `work`. Use `project:<name>` or do not write at all.

---

## See also

- [`README.md`](../README.md) — full feature list and quickstart
- [`docs/api.md`](api.md) — HTTP endpoint reference
- [`docs/adr/0007-minimal-token-auth.md`](adr/0007-minimal-token-auth.md) — why Bearer auth is the way it is
- [`examples/codex_cli/`](../examples/codex_cli/) — Codex CLI starter
- [`examples/shell/`](../examples/shell/) — curl starter
