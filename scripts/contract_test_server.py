#!/usr/bin/env python3
"""Start memory-hall for AMH contract tests (deterministic embedder, no Ollama)."""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import DeterministicEmbedder, build_settings  # noqa: E402
from memory_hall.server.app import create_app  # noqa: E402


def main() -> None:
    data_dir = Path("/tmp/memory-hall-contract")
    settings = build_settings(data_dir, tenant_id="default")
    settings.host = "127.0.0.1"
    settings.port = 19100
    settings.api_token = "contract-test-token"
    settings.prepare_paths()
    app = create_app(settings=settings, embedder=DeterministicEmbedder(dim=settings.vector_dim))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()