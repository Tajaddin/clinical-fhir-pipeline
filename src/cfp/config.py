"""Settings from environment variables (prefix CFP_) with laptop defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CFP_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Data
    synthea_dir: Path = Path("data/synthea")
    lakehouse_root: str = "data/lakehouse"
    reports_dir: Path = Path("reports")
    reference_date: str = "2025-01-01"  # Synthea's -r; ages and "recent" are relative to it

    # De-identification
    deid_salt: str = "change-me-to-a-long-random-string"
    date_shift_max_days: int = 30

    # Model
    readmission_days: int = 30
    test_size: float = 0.2
    val_size: float = 0.15
    seed: int = 42
    risk_threshold: float = 0.3  # operating point used for selection rates in the fairness table

    # API
    api_keys: str = ""  # "key:role,key:role"; roles analyst | admin
    rate_limit: str = "60/minute"
    database_url: str = "sqlite:///data/cfp.db"
    log_level: str = "INFO"
    service_name: str = "cfp-api"
    cors_origins: str = "http://localhost:4200"

    # LLM
    llm_base_url: str = ""  # OpenAI-compatible; empty = deterministic fake model
    llm_model: str = "qwen2.5:3b"
    llm_api_key: str = ""
    llm_timeout_s: float = 120.0
    embed_model: str = ""  # Ollama embedding model; empty = BM25 only
    facts_per_section: int = Field(default=8, ge=1, le=40)

    @property
    def api_key_roles(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in filter(None, (p.strip() for p in self.api_keys.split(","))):
            key, _, role = item.partition(":")
            out[key] = role or "analyst"
        return out

    @property
    def lakehouse_is_local(self) -> bool:
        return "://" not in self.lakehouse_root


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    settings.cache_clear()
