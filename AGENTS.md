# AGENTS.md

If you are an AI agent that just cloned this repo, read this first.

This file is **informational**, not a directive. It tells you where the agent-facing docs are. It does not tell you what to build.

---

## You are probably here to do one of these

1. **Write to / read from a running memhall instance** (most common).
   → Read [`docs/agent-integration.md`](docs/agent-integration.md). It has a decision tree that picks the right surface (embedded Python / HTTP+Bearer / `mh` CLI) based on whether your sandbox can open TCP sockets.

2. **Modify this codebase** (add a feature, fix a bug, write a test).
   → Read [`README.md`](README.md) (architecture and three entry points), [`docs/design.md`](docs/design.md) (internals), and [`docs/adr/`](docs/adr/) (why things are the way they are).

3. **Deploy memhall somewhere new.**
   → Read [`docs/deploy.md`](docs/deploy.md).

---

## Three things that bite agents on first contact

These are real failure modes seen in real agent sessions. Not hypothetical.

1. **`{"detail":"missing bearer token"}`** — the server has `MH_API_TOKEN` set. Every `/v1/memory/*` request needs `Authorization: Bearer ${MH_API_TOKEN}`. `/v1/health` is the only public endpoint.
2. **`command not found: mh`** — the CLI is a console script in `pyproject.toml`, it is not globally installed. Run `uv sync` inside the repo, then `uv run mh …`. The CLI reads `MH_API_TOKEN` from the environment automatically (no flag needed); export it before calling against an auth-enabled server.
3. **Sandboxed agents (Codex CLI, restricted containers) cannot open localhost TCP** — skip HTTP and use the embedded Python path (`from memory_hall import build_runtime`). Same storage, no auth, no network. See [`docs/agent-integration.md`](docs/agent-integration.md) Path A.

---

## What this file does NOT tell you

- It does not tell you to "rebuild the skeleton" or "follow this as the only source of truth". Treat the per-task instructions you were given as authoritative; this file just points at reference docs.
- It does not pin you to a phase or a workflow. The repo evolves. If anything in `docs/agent-integration.md` looks wrong against the running code, fix the doc — do not work around it.
