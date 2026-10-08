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

    app_env: Literal["dev", "test", "loadtest", "prod"] = "dev"
    log_level: str = "INFO"

    # --- infrastructure ---
    database_url: str = Field(..., description="postgresql://user:pass@host:5432/db")
    redis_url: str = Field(..., description="redis://host:6379/0")
    qdrant_url: str = Field(..., description="http(s)://host:6333, or ':memory:' in tests")
    qdrant_api_key: SecretStr | None = None
    pg_pool_max: int = Field(10, ge=1)  # per worker process; total = workers x this (put pgbouncer in front)
    direct_database_url: str | None = None  # session-mode URL for migrations when DATABASE_URL points at pgbouncer
    qdrant_collection: str = "enterprise_rag"  # ALIAS name; physical collections are <alias>_v<N>

    # --- LLM / gateway ---
    groq_api_key: SecretStr | None = None
    portkey_api_key: SecretStr | None = None
    groq_slug: str = "rag"
    groq_slug_fallback: str = "brag"
    llm_primary_model: str = "llama-3.3-70b-versatile"
    llm_fallback_model: str = "llama-3.1-8b-instant"
    guard_engine: Literal["actions", "runtime"] = "actions"  # "runtime" = full NeMo Colang flow engine (~20x more CPU)
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
    # --- retrieval (hybrid dense + sparse -> RRF -> rerank -> grade) ---
    prefetch_k: int = Field(40, ge=1)  # per-branch candidates (dense and sparse)
    fused_k: int = Field(20, ge=1)  # after server-side RRF fusion
    final_k: int = Field(5, ge=1)  # after rerank, sent to the LLM
    rerank_min_score: float = 0.05  # top rerank score below this => grade fails (rewrite + retry)
    rerank_workers: int = Field(2, ge=1)  # bounded thread pool for the ONNX reranker (per process)
    rerank_threads: int = Field(1, ge=1)  # ONNX intra-op threads per rerank batch (avoid CPU oversubscription)
    rerank_max_length: int = Field(512, ge=32, le=512)  # passages are truncated to this many tokens before scoring
    rerank_timeout_s: float = 10.0
    loadtest_real_reranker: bool = False  # APP_ENV=loadtest: use the real FlashRank instead of the no-op
    max_retrieval_iterations: int = Field(2, ge=1, le=5)
    sparse_backend: Literal["bm25", "simple"] = "bm25"  # fastembed Qdrant/bm25, or built-in hashing tokenizer + IDF
    # --- chunking ---
    chunk_target_tokens: int = Field(900, ge=100)
    chunk_overlap_ratio: float = Field(0.15, ge=0.0, le=0.5)
    chunk_min_tokens: int = Field(200, ge=1)  # a new heading only starts a new chunk once the current one has this many
    max_context_chars: int = 25_000

    # --- auth lookup cost ---
    key_cache_ttl_s: float = 30.0  # valid-key LRU TTL (revocation also evicts immediately via Redis pub/sub)
    key_cache_size: int = Field(10_000, ge=1)
    key_neg_ttl_s: int = 60  # an unknown key hash is remembered in Redis this long (no Postgres lookup)
    auth_fail_per_min: int = Field(20, ge=1)  # failed-auth attempts per client IP per minute before 429
    trusted_proxies: str = ""  # comma-separated IPs/CIDRs allowed to set X-Forwarded-For

    # --- memory ---
    memory_ttl_days: int = Field(30, ge=1)
    planner_cache_ttl_s: int = 3600
    embed_cache_ttl_s: int = 7 * 86400
    semcache_enabled: bool = True
    semcache_threshold: float = 0.95
    semcache_ttl_s: int = 86400
    tenant_config_ttl_s: int = 60
    web_concurrency: int = Field(1, ge=1)  # used to size the in-process rate-limit fallback
    metrics_port: int = 9100

    # --- loadtest (APP_ENV=loadtest): simulated upstream latency in ms ---
    loadtest_llm_ms: float = 800
    loadtest_llm_jitter_ms: float = 300
    loadtest_embed_ms: float = 80
    loadtest_embed_jitter_ms: float = 30
    loadtest_guard_ms: float = 250

    # --- ingestion ---
    ingest_batch_size: int = Field(128, ge=1)
    ingest_root: str = "DATA"
    keep_old_collections: int = 1
    rebuild_lock_ttl_s: int = Field(60, ge=3)  # heartbeat renews it while a rebuild is alive; expiry = crashed holder

    assistant_domain: str = "Kubernetes, Intel hardware and enterprise networking"

    # --- observability ---
    logfire_token: SecretStr | None = None
    langsmith_tracing: bool = False  # when true, LangGraph runs are traced with PII-redacted inputs/outputs

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
