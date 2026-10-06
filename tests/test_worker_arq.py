"""Real arq round-trip over the Redis protocol: enqueue -> worker executes the shared ingest()."""
import os
import sys
import uuid

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker

from app.worker import cleanup_job, ingest_job


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("REDIS_INTEGRATION_URL"), reason="needs a real Redis (docker compose); fakeredis TCP server cannot run arq")
async def test_arq_enqueue_and_execute(make_app, tmp_path):
    redis_url = os.environ["REDIS_INTEGRATION_URL"]
    t = f"t{uuid.uuid4().hex[:6]}"
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.txt").write_text("kubernetes pods are scheduled onto nodes. " * 5)
    async with make_app() as e:
        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        job = await pool.enqueue_job("ingest_job", t, str(d), None, False)
        ctx_extra = {"vs": e.svc.vectors, "embedder": e.embedder, "s": e.settings}

        async def startup(ctx):
            ctx.update(ctx_extra)

        w = Worker(functions=[ingest_job], cron_jobs=[], redis_pool=pool, burst=True, on_startup=startup,
                   max_tries=1, handle_signals=False)
        await w.main()
        res = await job.result(timeout=5)
        assert res["files"] == 1 and res["embedded"] > 0
        if sys.platform != "win32":  # arq's close() references SIGUSR1 (POSIX only); job already completed
            await w.close()
        await pool.aclose()


def test_cleanup_cron_registered():
    from app.worker import WorkerSettings

    assert any(c.coroutine is cleanup_job for c in WorkerSettings.cron_jobs)
