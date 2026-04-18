from __future__ import annotations

from pathlib import Path

from pydantic import Field
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
    embed_timeout_s: float = 2.0
    vector_dim: int = 1024
    default_tenant_id: str = "default"
    list_default_limit: int = 50
    search_default_limit: int = 20
    search_candidate_multiplier: int = 5
    request_timeout_s: float = 5.0
    reindex_batch_size: int = 500

    def prepare_paths(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_database_path.parent.mkdir(parents=True, exist_ok=True)
