"""arq worker:  arq app.worker.WorkerSettings   (ingestion jobs + idle-thread cleanup cron)"""
from __future__ import annotations

import asyncio
import os

from arq import cron
from arq.connections import RedisSettings

from app.config import load_settings
from app.db.cleanup import cleanup_idle_threads_sync
from app.errors import AppError
from app.ingestion.pipeline import ingest
from app.resilience import Breakers
from app.services.vectorstore import VectorStore, make_client


async def startup(ctx):
    s = load_settings()
    b = Breakers(s.breaker_failures, s.breaker_reset_s)
    ctx["s"] = s
    ctx["client"] = make_client(s)
    from redis import asyncio as aioredis

    from app.services.rebuild import RebuildCoordinator
    from app.services.semcache import SemanticCache
    from app.services.sparse import make_sparse_encoder

    ctx["redis"] = aioredis.from_url(s.redis_url)
    coord = RebuildCoordinator(ctx["redis"], s.qdrant_collection, s.direct_database_url or s.database_url, s.rebuild_lock_ttl_s)
    ctx["vs"] = VectorStore(ctx["client"], s, b, coord)
    ctx["semcache"] = SemanticCache(ctx["client"], s)
    if "embedder" not in ctx:  # tests pre-seed a fake
        from app.services.embedding import GeminiEmbedder
        from app.services.embedding_cache import CachedEmbedder

        if s.app_env == "loadtest":
            from app.loadtest import LoadEmbedder

            inner = LoadEmbedder(s)
        else:
            inner = GeminiEmbedder(s, b)
        ctx["embedder"] = CachedEmbedder(inner, ctx["redis"], s.embed_cache_ttl_s)
    if "sparse" not in ctx:
        ctx["sparse"] = make_sparse_encoder(s.sparse_backend)


async def shutdown(ctx):
    await ctx["client"].close()
    await ctx["redis"].aclose()


async def ingest_job(ctx, tenant_id: str, path: str, source_type: str | None = None, rebuild: bool = False):
    rep = await ingest(ctx["vs"], ctx["embedder"], ctx["sparse"], ctx["s"], tenant_id, path, source_type, rebuild)
    if not rep.ok:
        raise AppError(f"ingestion finished with {len(rep.failed)} failed files: {rep.failed[:5]}")
    return {"files": rep.files_ok, "chunks": rep.chunks_total, "embedded": rep.chunks_embedded}


async def cleanup_job(ctx):
    s = ctx["s"]
    return await asyncio.to_thread(cleanup_idle_threads_sync, s.database_url, s.memory_ttl_days)


async def semcache_cleanup(ctx):
    await ctx["semcache"].purge_expired()


class WorkerSettings:
    functions = [ingest_job]
    cron_jobs = [cron(cleanup_job, hour={3}, minute={17}), cron(semcache_cleanup, minute={5, 35})]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 1
    job_timeout = 3600
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
