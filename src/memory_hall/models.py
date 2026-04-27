from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SYNC_PENDING = "pending"
SYNC_EMBEDDED = "embedded"
SYNC_FAILED = "failed"
SyncStatus = Literal["pending", "embedded", "failed"]
SemanticStatus = Literal["ok", "timeout", "embedder_error", "not_attempted"]

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_entry_id(now_ms: int | None = None) -> str:
    timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    value = (timestamp_ms << 80) | secrets.randbits(80)
    chars: list[str] = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def build_content_hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def encode_cursor(created_at: datetime, entry_id: str) -> str:
    raw = f"{created_at.isoformat()}|{entry_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    created_at_raw, entry_id = decoded.split("|", 1)
    return datetime.fromisoformat(created_at_raw), entry_id


def dump_json(data: list[str] | dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True, frozen=True)
class Entry:
    entry_id: str
    tenant_id: str
    agent_id: str
    namespace: str
    type: str
    content: str
    content_hash: str
    summary: str | None
    tags: list[str]
    references: list[str]
    metadata: dict[str, Any]
    sync_status: SyncStatus
    last_embedded_at: datetime | None
    last_embed_error: str | None
    last_embed_attempted_at: datetime | None
    embed_attempt_count: int
    created_at: datetime
    created_by_principal: str


@dataclass(slots=True, frozen=True)
class InsertOutcome:
    entry: Entry
    created: bool


@dataclass(slots=True, frozen=True)
class SearchCandidate:
    entry_id: str
    score: float


@dataclass(slots=True, frozen=True)
class WriteOutcome:
    entry: Entry
    created: bool
    embedded: bool
    status_code: int


class EntryDocument(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entry_id: str
    tenant_id: str
    agent_id: str
    namespace: str
    type: str
    content: str
    content_hash: str
    summary: str | None
    tags: list[str]
    references: list[str]
    metadata: dict[str, Any]
    sync_status: SyncStatus
    last_embedded_at: datetime | None
    last_embed_error: str | None
    last_embed_attempted_at: datetime | None
    embed_attempt_count: int
    created_at: datetime
    created_by_principal: str

    @classmethod
    def from_entry(cls, entry: Entry) -> EntryDocument:
        return cls.model_validate(entry)


class WriteMemoryRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    type: str = Field(min_length=1)
    content: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("references")
    @classmethod
    def normalize_references(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
        return normalized


class WriteMemoryResponse(BaseModel):
    entry_id: str
    embedded: bool
    indexed_at: datetime | None
    sync_status: SyncStatus
    created: bool

    @classmethod
    def from_outcome(cls, outcome: WriteOutcome) -> WriteMemoryResponse:
        return cls(
            entry_id=outcome.entry.entry_id,
            embedded=outcome.embedded,
            indexed_at=outcome.entry.last_embedded_at,
            sync_status=outcome.entry.sync_status,
            created=outcome.created,
        )


class SearchMemoryRequest(BaseModel):
    query: str = Field(min_length=1)
    namespace: list[str] | None = None
    agent_id: str | None = None
    type: list[str] | None = None
    tags: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"


class ScoreBreakdown(BaseModel):
    bm25: float
    semantic: float
    rrf: float
    semantic_status: SemanticStatus = "not_attempted"


class SearchResultItem(BaseModel):
    entry_id: str
    score: float
    score_breakdown: ScoreBreakdown
    entry: EntryDocument


class SearchMemoryResponse(BaseModel):
    results: list[SearchResultItem]
    total: int
    degraded: bool = False


class GetEntryResponse(BaseModel):
    entry: EntryDocument
    references_out: list[EntryDocument]
    references_in: list[EntryDocument]


class LinkEntryRequest(BaseModel):
    target_entry_id: str = Field(min_length=1)
    relation: str | None = None


class LinkEntryResponse(BaseModel):
    entry: EntryDocument


class ListEntriesResponse(BaseModel):
    entries: list[EntryDocument]
    next_cursor: str | None


class ReindexResponse(BaseModel):
    scanned: int
    embedded: int
    pending: int


class AuditResponse(BaseModel):
    total_entries: int
    tenant_counts: dict[str, int]
    namespace_counts: dict[str, int]
    sync_status_counts: dict[str, int]
    content_hash_collisions: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    storage: str
    vector_store: str
    embedder: str
    last_success_at: datetime | None = None
    last_error: str | None = None
