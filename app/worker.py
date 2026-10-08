"""arq worker:  arq app.worker.WorkerSettings   (ingestion jobs + idle-thread cleanup cron)"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from arq import cron
from arq.connections import RedisSettings
from arq.constants import default_queue_name

from app.config import load_settings
from app.db.cleanup import cleanup_idle_threads_sync
from app.errors import AppError
from app.ingestion.pipeline import ingest
from app.observability.metrics import INGEST_QUEUE_DEPTH, WORKER_HEARTBEAT
from app.resilience import Breakers
from app.services.vectorstore import VectorStore, make_client

log = logging.getLogger("app.worker")
METRICS_REFRESH_S = 15.0
_metrics_server_started = False


async def refresh_queue_metrics(redis, queue_name: str = default_queue_name) -> int:
    """rag_ingest_queue_depth = ZCARD of the arq queue; the heartbeat is stamped only after Redis answered."""
    depth = int(await redis.zcard(queue_name))
    INGEST_QUEUE_DEPTH.set(depth)
    WORKER_HEARTBEAT.set_to_current_time()
    return depth


async def _metrics_loop(redis, queue_name: str = default_queue_name, interval: float = METRICS_REFRESH_S) -> None:
    """Per-process heartbeat (not an arq cron: a cron job is claimed by ONE replica, the others' gauges would go stale)."""
    while True:
        try:
            await refresh_queue_metrics(redis, queue_name)
        except Exception as e:  # Redis blip: keep looping; the heartbeat simply stops advancing
            log.warning("queue-depth refresh failed: %s", e)
        await asyncio.sleep(interval)


def start_metrics_server() -> bool:
    """Serve this worker's metrics on WORKER_METRICS_PORT (default 9101, 0 disables). Never fatal: ingestion matters more."""
    global _metrics_server_started
    if _metrics_server_started:
        return True
    raw = os.environ.get("WORKER_METRICS_PORT", "9101")
    try:
        port = int(raw)
        if port <= 0:
            return False
        from app.metrics_server import start

        start(port)
    except (ValueError, OSError) as e:
        log.warning("worker metrics server not started (WORKER_METRICS_PORT=%r): %s", raw, e)
        return False
    _metrics_server_started = True
    return True


async def startup(ctx):
    s = load_settings()
    b = Breakers(s.breaker_failures, s.breaker_reset_s)
    ctx["s"] = s
    ctx["client"] = make_client(s)
    start_metrics_server()
    from redis import asyncio as aioredis

    from app.services.rebuild import RebuildCoordinator
    from app.services.semcache import SemanticCache
    from app.services.sparse import make_sparse_encoder

    ctx["redis"] = aioredis.from_url(s.redis_url)
    ctx["metrics_task"] = asyncio.create_task(_metrics_loop(ctx["redis"]))
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
    task = ctx.pop("metrics_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
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
