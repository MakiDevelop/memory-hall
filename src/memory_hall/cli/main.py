# ruff: noqa: B008

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from memory_hall.config import Settings
from memory_hall.server.app import create_app
from memory_hall.storage.sqlite_store import SqliteStore

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _settings() -> Settings:
    return Settings()


def _client(base_url: str, timeout_s: float) -> httpx.Client:
    return httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)


def _parse_metadata(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise typer.BadParameter("metadata must be a JSON object")
    return parsed


@app.command()
def serve(
    host: str | None = typer.Option(default=None),
    port: int | None = typer.Option(default=None),
    database_path: Path | None = typer.Option(default=None),
    vector_database_path: Path | None = typer.Option(default=None),
) -> None:
    settings = _settings()
    if host is not None:
        settings.host = host
    if port is not None:
        settings.port = port
    if database_path is not None:
        settings.database_path = database_path
    if vector_database_path is not None:
        settings.vector_database_path = vector_database_path
    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port)


@app.command()
def write(
    content: str = typer.Argument(...),
    agent_id: str = typer.Option("codex"),
    namespace: str = typer.Option("shared"),
    type: str = typer.Option("note"),
    summary: str | None = typer.Option(default=None),
    tag: list[str] | None = typer.Option(default=None),
    reference: list[str] | None = typer.Option(default=None),
    metadata: str | None = typer.Option(default=None),
    base_url: str | None = typer.Option(default=None),
) -> None:
    settings = _settings()
    target = base_url or settings.api_base_url
    payload = {
        "agent_id": agent_id,
        "namespace": namespace,
        "type": type,
        "content": content,
        "summary": summary,
        "tags": tag or [],
        "references": reference or [],
        "metadata": _parse_metadata(metadata),
    }
    with _client(target, settings.request_timeout_s) as client:
        response = client.post("/v1/memory/write", json=payload)
        response.raise_for_status()
    console.print(JSON(response.text))


@app.command()
def search(
    query: str = typer.Argument(...),
    namespace: list[str] | None = typer.Option(default=None),
    agent_id: str | None = typer.Option(default=None),
    type: list[str] | None = typer.Option(default=None),
    tags: list[str] | None = typer.Option(default=None),
    limit: int = typer.Option(default=10, min=1, max=100),
    mode: str = typer.Option(default="hybrid"),
    base_url: str | None = typer.Option(default=None),
) -> None:
    settings = _settings()
    target = base_url or settings.api_base_url
    payload = {
        "query": query,
        "namespace": namespace,
        "agent_id": agent_id,
        "type": type,
        "tags": tags,
        "limit": limit,
        "mode": mode,
    }
    with _client(target, settings.request_timeout_s) as client:
        response = client.post("/v1/memory/search", json=payload)
        response.raise_for_status()
        data = response.json()
    table = Table(title=f"memory-hall search: {query}")
    table.add_column("entry_id")
    table.add_column("score", justify="right")
    table.add_column("type")
    table.add_column("content")
    for item in data["results"]:
        table.add_row(
            item["entry_id"],
            f"{item['score']:.4f}",
            item["entry"]["type"],
            item["entry"]["content"],
        )
    console.print(table)


@app.command()
def get(
    entry_id: str = typer.Argument(...),
    base_url: str | None = typer.Option(default=None),
) -> None:
    settings = _settings()
    target = base_url or settings.api_base_url
    with _client(target, settings.request_timeout_s) as client:
        response = client.get(f"/v1/memory/{entry_id}")
        response.raise_for_status()
    console.print(JSON(response.text))


@app.command()
def tail(
    limit: int = typer.Option(default=20, min=1, max=200),
    interval_s: float = typer.Option(default=2.0, min=0.2),
    namespace: list[str] | None = typer.Option(default=None),
    agent_id: str | None = typer.Option(default=None),
    type: list[str] | None = typer.Option(default=None),
    base_url: str | None = typer.Option(default=None),
) -> None:
    settings = _settings()
    target = base_url or settings.api_base_url
    seen: set[str] = set()
    with _client(target, settings.request_timeout_s) as client:
        while True:
            response = client.get(
                "/v1/memory",
                params={
                    "limit": limit,
                    "namespace": namespace,
                    "agent_id": agent_id,
                    "type": type,
                },
            )
            response.raise_for_status()
            data = response.json()
            fresh = [item for item in reversed(data["entries"]) if item["entry_id"] not in seen]
            for item in fresh:
                seen.add(item["entry_id"])
                console.print(f"[{item['created_at']}] {item['entry_id']} {item['content']}")
            time.sleep(interval_s)


@app.command("reindex-fts")
def reindex_fts(
    tenant_id: str | None = typer.Option(default=None),
    batch_size: int = typer.Option(default=500, min=1, max=5000),
    database_path: Path | None = typer.Option(default=None),
) -> None:
    asyncio.run(
        _reindex_fts(
            tenant_id=tenant_id,
            batch_size=batch_size,
            database_path=database_path,
        )
    )


async def _reindex_fts(
    *,
    tenant_id: str | None,
    batch_size: int,
    database_path: Path | None,
) -> None:
    settings = _settings()
    if database_path is not None:
        settings.database_path = database_path
    active_tenant_id = tenant_id or settings.default_tenant_id
    store = SqliteStore(settings.database_path)
    await store.open()
    try:
        entries = await store.list_entries(active_tenant_id, limit=None)
        entries.reverse()
        scanned = 0
        reindexed = 0
        for offset in range(0, len(entries), batch_size):
            batch = entries[offset : offset + batch_size]
            scanned += len(batch)
            reindexed += await store.reindex_fts_entries(batch)
            console.print(
                f"tenant={active_tenant_id} scanned={scanned}/{len(entries)} reindexed={reindexed}"
            )
    finally:
        await store.close()
