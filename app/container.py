"""Service wiring. Everything external is constructed here so tests can inject fakes via `overrides`."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import logfire
from langgraph.checkpoint.memory import MemorySaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis import asyncio as aioredis

from app.agents.graph import build_graph
from app.auth.keys import KeyStore, TenantConfig
from app.config import Settings
from app.errors import EmbeddingConfigMismatch
from app.guardrails import Guardrails
from app.ratelimit import RateLimiter
from app.resilience import Breakers
from app.services.vectorstore import VectorStore, make_client


@dataclass
class Services:
    settings: Settings
    breakers: Breakers
    pool: AsyncConnectionPool
    redis: Any
    keystore: KeyStore
    limiter: RateLimiter
    vectors: VectorStore
    sparse: Any
    semcache: Any
    tenant_config: TenantConfig
    embedder: Any
    llm: Any
    reranker: Any
    guard: Guardrails
    checkpointer: Any
    graph: Any = None
    arq: Any = None
    _closers: list = field(default_factory=list)

    async def aclose(self) -> None:
        for c in reversed(self._closers):
            try:
                await c()
            except Exception as e:  # shutdown must not raise
                logfire.warn("shutdown error", error=str(e))


async def build_services(s: Settings, overrides: dict | None = None) -> Services:
    ov = overrides or {}
    breakers = Breakers(s.breaker_failures, s.breaker_reset_s)
    closers: list = []

    pool = AsyncConnectionPool(
        s.database_url, min_size=1, max_size=int(ov.get("pg_pool_max", s.pg_pool_max)), open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    closers.append(pool.close)

    redis = aioredis.from_url(s.redis_url)
    closers.append(redis.aclose)

    qclient = ov.get("qdrant_client") or make_client(s)
    if "qdrant_client" not in ov:  # injected clients are owned (and closed) by the caller
        closers.append(qclient.close)
    vectors = VectorStore(qclient, s, breakers)
    try:
        await vectors.verify_config()  # refuse to start on embedding model/dim mismatch
        try:
            await vectors.ensure_active()  # first boot: empty v1 + alias (a racing worker may win; that is fine)
        except EmbeddingConfigMismatch:
            raise
        except Exception as e:
            logfire.info("ensure_active skipped", error=str(e))
    except EmbeddingConfigMismatch:
        await _close_all(closers)
        raise
    except Exception as e:  # Qdrant merely unreachable: boot, report via /readyz
        logfire.warn("qdrant not reachable at startup", error=str(e))

    from app.services.embedding_cache import CachedEmbedder
    from app.services.semcache import SemanticCache
    from app.services.sparse import make_sparse_encoder

    loadtest = s.app_env == "loadtest"
    if "embedder" in ov:
        embedder = ov["embedder"]
    elif loadtest:
        from app.loadtest import LoadEmbedder

        embedder = LoadEmbedder(s)
    else:
        from app.services.embedding import GeminiEmbedder

        embedder = GeminiEmbedder(s, breakers)
    embedder = CachedEmbedder(embedder, redis, s.embed_cache_ttl_s)
    if "llm" in ov:
        llm = ov["llm"]
    elif loadtest:
        from app.loadtest import LoadLLM

        llm = LoadLLM(s)
    else:
        from app.services.llm import PortkeyLLM

        llm = PortkeyLLM(s, breakers)
    sparse = ov.get("sparse") or make_sparse_encoder(s.sparse_backend)
    if "reranker" in ov:
        reranker = ov["reranker"]
    elif loadtest and not ov.get("real_reranker"):
        from app.services.reranker import NoopReranker

        reranker = NoopReranker()
    else:
        from app.services.reranker import FlashRankReranker, NoopReranker

        try:
            reranker = await asyncio.to_thread(FlashRankReranker)
        except Exception as e:
            logfire.error("FlashRank unavailable; reranking disabled", error=str(e))
            reranker = NoopReranker()

    guard = ov.get("guard") or Guardrails(s, breakers)
    if not guard.ready:
        guard_llm = ov.get("guard_llm")
        if guard_llm is None and loadtest:
            from app.loadtest import LoadGuardLLM

            guard_llm = LoadGuardLLM(s)
        await asyncio.to_thread(guard.initialize, guard_llm)

    if s.app_env == "test":
        checkpointer = MemorySaver()  # the ONLY place MemorySaver is allowed
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer = AsyncPostgresSaver(pool)  # tables created by `python -m app.db.migrate`

    svc = Services(
        settings=s, breakers=breakers, pool=pool, redis=redis, keystore=KeyStore(pool, ov.get("key_cache_ttl", 10.0)),
        limiter=RateLimiter(redis, workers=s.web_concurrency), vectors=vectors, sparse=sparse, semcache=SemanticCache(qclient, s),
        tenant_config=TenantConfig(pool, s.assistant_domain, s.tenant_config_ttl_s), embedder=embedder, llm=llm, reranker=reranker,
        guard=guard, checkpointer=checkpointer, _closers=closers,
    )
    svc.graph = build_graph(svc, checkpointer)
    return svc


async def _close_all(closers):
    for c in reversed(closers):
        try:
            await c()
        except Exception as e:
            logfire.warn("close failed during startup abort", error=str(e))
