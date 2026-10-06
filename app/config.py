"""Typed, validated settings. Fails fast at startup with readable messages."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # --- infrastructure ---
    database_url: str = Field(..., description="postgresql://user:pass@host:5432/db")
    redis_url: str = Field(..., description="redis://host:6379/0")
    qdrant_url: str = Field(..., description="http(s)://host:6333, or ':memory:' in tests")
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "enterprise_rag"  # ALIAS name; physical collections are <alias>_v<N>

    # --- LLM / gateway ---
    groq_api_key: SecretStr | None = None
    portkey_api_key: SecretStr | None = None
    groq_slug: str = "rag"
    groq_slug_fallback: str = "brag"
    llm_primary_model: str = "llama-3.3-70b-versatile"
    llm_fallback_model: str = "llama-3.1-8b-instant"
    guard_model: str = "llama-3.1-8b-instant"

    # --- embeddings (Gemini GA) ---
    gemini_api_key: SecretStr | None = None
    embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = Field(768, ge=128, le=3072)

    # --- timeouts (seconds) ---
    llm_timeout_s: float = 30.0
    embed_timeout_s: float = 10.0
    qdrant_timeout_s: float = 5.0
    max_attempts: int = Field(3, ge=1, le=5)
    breaker_failures: int = 5
    breaker_reset_s: float = 30.0

    # --- concurrency / limits ---
    llm_concurrency: int = Field(32, ge=1)
    default_rate_per_min: int = 60
    default_rate_per_day: int = 10_000
    max_query_chars: int = 4000
    retrieval_candidates: int = 15
    retrieval_top_n: int = 5
    max_context_chars: int = 25_000

    # --- memory ---
    memory_ttl_days: int = Field(30, ge=1)
    planner_cache_ttl_s: int = 3600

    # --- ingestion ---
    ingest_batch_size: int = Field(128, ge=1)
    ingest_root: str = "DATA"
    keep_old_collections: int = 1

    assistant_domain: str = "Kubernetes, Intel hardware and enterprise networking"

    # --- observability ---
    logfire_token: SecretStr | None = None

    @model_validator(mode="after")
    def _cross_checks(self):
        if self.app_env == "prod":
            missing = [n for n in ("groq_api_key", "portkey_api_key", "gemini_api_key") if not getattr(self, n)]
            if missing:
                raise ValueError(f"APP_ENV=prod requires: {', '.join(m.upper() for m in missing)}")
            if self.qdrant_url == ":memory:":
                raise ValueError("QDRANT_URL=':memory:' is not allowed in prod")
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must start with postgresql://")
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return self


def load_settings(**overrides) -> Settings:
    """Build settings; convert pydantic errors into one clear message."""
    try:
        return Settings(**overrides)
    except ValidationError as e:
        lines = [
            f"  - {'.'.join(str(p) for p in err['loc']).upper() or 'SETTINGS'}: {err['msg']}"
            for err in e.errors()
        ]
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
