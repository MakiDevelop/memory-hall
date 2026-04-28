from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MH_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 9000
    api_base_url: str = "http://127.0.0.1:9000"
    database_path: Path = Field(default=Path(".data/memory-hall.sqlite3"))
    vector_database_path: Path = Field(default=Path(".data/memory-hall-vectors.sqlite3"))
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "bge-m3"
    embed_timeout_s: float = 8.0
    health_embed_timeout_s: float = 3.0
    search_embed_timeout_s: float = 2.0
    embedder_kind: Literal["ollama", "http"] = "ollama"
    embed_base_url: str | None = None
    embed_dim: int | None = None
    vector_dim: int = 1024
    default_tenant_id: str = "default"
    api_token: str | None = None
    admin_token: str | None = None
    list_default_limit: int = 50
    search_default_limit: int = 20
    search_candidate_multiplier: int = 5
    hybrid_mode: Literal["weighted_linear", "rrf"] = "rrf"
    hybrid_alpha: float = Field(default=0.3, ge=0.0, le=1.0)
    request_timeout_s: float = 5.0
    reindex_batch_size: int = 500
    wal_checkpoint_interval_s: float = 300.0
    max_content_bytes: int = 64 * 1024

    @model_validator(mode="before")
    @classmethod
    def _sync_input_dims(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        vector_dim = values.get("vector_dim")
        embed_dim = values.get("embed_dim")
        if embed_dim is None and vector_dim is not None:
            values["embed_dim"] = vector_dim
        elif vector_dim is None and embed_dim is not None:
            values["vector_dim"] = embed_dim
        elif embed_dim is not None and vector_dim is not None and embed_dim != vector_dim:
            raise ValueError("embed_dim and vector_dim must match")
        return values

    @model_validator(mode="after")
    def _set_default_embed_dim(self) -> Settings:
        if self.embed_dim is None:
            self.embed_dim = self.vector_dim
        return self

    def prepare_paths(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_database_path.parent.mkdir(parents=True, exist_ok=True)
