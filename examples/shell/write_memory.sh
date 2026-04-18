#!/usr/bin/env bash
set -euo pipefail

curl -sS http://127.0.0.1:9000/v1/memory/write \
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
