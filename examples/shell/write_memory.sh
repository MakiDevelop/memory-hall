#!/usr/bin/env bash
set -euo pipefail

# When the server has MH_API_TOKEN set, every /v1/memory/* request needs
# `Authorization: Bearer <token>`. /v1/health is the only public endpoint.
# Maki's setup keeps the token at ~/.config/memhall/token (0600).
#
# If the server runs without MH_API_TOKEN (dev / standalone), the header is
# ignored — sending it anyway is safe and forward-compatible.
TOKEN="${MH_API_TOKEN:-$(cat ~/.config/memhall/token 2>/dev/null || true)}"

curl -sS http://127.0.0.1:9000/v1/memory/write \
  ${TOKEN:+-H "Authorization: Bearer ${TOKEN}"} \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "codex",
    "namespace": "shared",
    "type": "note",
    "content": "memory-hall v0.1 quickstart write via curl",
    "summary": "curl example",
    "tags": ["quickstart", "shell"],
    "references": [],
    "metadata": {"source": "examples/shell/write_memory.sh"}
  }'
