from __future__ import annotations

from datetime import datetime
from typing import Protocol

from memory_hall.models import Entry, InsertOutcome, SearchCandidate


class Storage(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def healthcheck(self) -> None: ...

    async def insert_entry(self, entry: Entry) -> InsertOutcome: ...

    async def update_sync_status(
        self,
        tenant_id: str,
        entry_id: str,
        sync_status: str,
        last_embedded_at: datetime | None,
        last_embed_error: str | None,
        last_embed_attempted_at: datetime | None,
        embed_attempt_count: int,
    ) -> Entry | None: ...

    async def get_entry(self, tenant_id: str, entry_id: str) -> Entry | None: ...

    async def get_entry_by_hash(self, tenant_id: str, content_hash: str) -> Entry | None: ...

    async def get_entries_by_ids(self, tenant_id: str, entry_ids: list[str]) -> list[Entry]: ...

    async def list_entries(
        self,
        tenant_id: str,
        *,
        namespaces: list[str] | None = None,
        agent_id: str | None = None,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        sync_status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[Entry]: ...

    async def count_entries(
        self,
        tenant_id: str,
        *,
        sync_status: str | None = None,
    ) -> int: ...

    async def search_lexical(
        self,
        tenant_id: str,
        query: str,
        *,
        namespaces: list[str] | None = None,
        agent_id: str | None = None,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[SearchCandidate]: ...

    async def add_reference(
        self,
        tenant_id: str,
        source_entry_id: str,
        target_entry_id: str,
    ) -> Entry | None: ...

    async def list_pending_entries(
        self, tenant_id: str, limit: int | None = None
    ) -> list[Entry]: ...

    async def list_tenant_ids(self) -> list[str]: ...

    async def get_references_out(self, tenant_id: str, entry_id: str) -> list[Entry]: ...

    async def get_references_in(self, tenant_id: str, entry_id: str) -> list[Entry]: ...

    async def audit(self) -> dict[str, object]: ...
