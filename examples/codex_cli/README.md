# Codex CLI

`mh` wraps the HTTP API, so Codex or any shell session can write notes without touching MCP.

Start the server:

```bash
uv run python -m memory_hall serve
```

Write a note:

```bash
uv run mh write "DEC-018 落地完成" \
  --agent-id codex \
  --namespace project:memory-hall \
  --type decision \
  --tag governance
```

Search:

```bash
uv run mh search "DEC-018"
```
