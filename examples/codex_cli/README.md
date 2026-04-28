# Codex CLI

`mh` wraps the HTTP API, so Codex or any shell session can write notes without touching MCP.

> **Auth status (verified 2026-04-28 against `fix/reliability-phase-a5-2026-04-27`)**: the CLI reads `MH_API_TOKEN` from the environment via `Settings()` and attaches `Authorization: Bearer <token>` automatically. Works against both auth-enabled and no-auth servers. See [`docs/agent-integration.md`](../../docs/agent-integration.md) for the full decision tree.

## Install (one-time)

`mh` is a console script defined in `pyproject.toml`. It is **not** globally available until the package is installed in this venv.

```bash
uv sync
```

If `command -v mh` still returns nothing, you are not in the project venv. `uv run mh …` invokes it without activation.

In sandboxes where `~/.cache/uv` is not writable (e.g. Codex CLI restricted environments), set `UV_CACHE_DIR=/tmp/uv-cache` before calling `uv`.

## Start the server

```bash
uv run python -m memory_hall serve
```

## Auth

The CLI reads `MH_API_TOKEN` from the environment on each command via `Settings()` and attaches it as `Authorization: Bearer <token>`. No flag, no manual header.

Maki's setup keeps the token at `~/.config/memhall/token` (0600). Export before calling:

```bash
export MH_API_TOKEN="$(cat ~/.config/memhall/token)"
```

If `MH_API_TOKEN` is unset, no `Authorization` header is sent — `mh` works against no-auth dev servers unchanged.

Implementation: `src/memory_hall/cli/main.py:31` (`_client()` injects the header from `Settings().api_token`). Test coverage: `tests/test_cli_auth.py`.

## Write a note

```bash
uv run mh write "DEC-018 落地完成" \
  --agent-id codex \
  --namespace project:memory-hall \
  --type decision \
  --tag governance
```

## Search

```bash
uv run mh search "DEC-018"
```
