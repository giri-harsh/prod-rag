"""Q4 observability metrics: every metric the alert rules / dashboard rely on is exported and moves as designed.

New here: rag_circuit_breaker_open / rag_circuit_breaker_trips_total (app/resilience.py), rag_ready / rag_ready_check
(GET /readyz), rag_ingest_queue_depth / rag_worker_heartbeat_timestamp_seconds (app/worker.py), plus the multiprocess
gauge modes they depend on and the prometheus-fastapi-instrumentator names the rules query (status is GROUPED: "5xx").
"""
import asyncio
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis
import httpx
import pytest
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest, multiprocess

from app.auth.keys import Principal
from app.errors import CircuitOpen
from app.ratelimit import RateLimiter
from app.resilience import CircuitBreaker, call_upstream
from app.worker import _metrics_loop, refresh_queue_metrics, start_metrics_server
from tests.conftest import H

ROOT = Path(__file__).resolve().parents[1]


def sample(name: str, **labels):
    return REGISTRY.get_sample_value(name, labels)


def uniq(prefix: str = "t") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------ circuit breaker gauge + trips counter
def test_breaker_gauge_and_trips_follow_state():
    name, now = uniq(), {"t": 0.0}
    b = CircuitBreaker(name, failures=2, reset_s=30, clock=lambda: now["t"])
    assert sample("rag_circuit_breaker_open", upstream=name) == 0.0  # exported from creation: "closed", not "no data"
    assert sample("rag_circuit_breaker_trips_total", upstream=name) == 0.0

    b.record_failure()
    assert sample("rag_circuit_breaker_open", upstream=name) == 0.0 and sample("rag_circuit_breaker_trips_total", upstream=name) == 0.0
    b.record_failure()  # threshold reached: closed -> open
    assert sample("rag_circuit_breaker_open", upstream=name) == 1.0 and sample("rag_circuit_breaker_trips_total", upstream=name) == 1.0
    b.record_failure()  # a call that was already in flight fails after the breaker opened: not a new trip
    assert sample("rag_circuit_breaker_trips_total", upstream=name) == 1.0

    now["t"] = 31.0  # half-open: a probe is let through, the gauge stays 1 until the upstream actually recovers
    b.before_call()
    assert b.state == "half-open" and sample("rag_circuit_breaker_open", upstream=name) == 1.0
    b.record_failure()  # failed probe -> re-opened = a second trip
    assert sample("rag_circuit_breaker_open", upstream=name) == 1.0 and sample("rag_circuit_breaker_trips_total", upstream=name) == 2.0

    now["t"] = 70.0
    b.before_call()
    b.record_success()  # successful probe closes it
    assert b.state == "closed"
    assert sample("rag_circuit_breaker_open", upstream=name) == 0.0 and sample("rag_circuit_breaker_trips_total", upstream=name) == 2.0


async def test_call_upstream_failures_open_the_gauge_and_recovery_closes_it():
    name = uniq()
    b = CircuitBreaker(name, failures=2, reset_s=0.05)

    async def boom():
        raise ValueError("upstream exploded")  # not retryable -> exactly one attempt per call

    async def fine():
        return "ok"

    for _ in range(2):
        with pytest.raises(ValueError):
            await call_upstream(b, boom, timeout_s=1, attempts=1)
    assert sample("rag_circuit_breaker_open", upstream=name) == 1.0 and sample("rag_circuit_breaker_trips_total", upstream=name) == 1.0
    with pytest.raises(CircuitOpen):  # rejected by the open breaker without touching the upstream
        await call_upstream(b, fine, timeout_s=1, attempts=1)
    assert sample("rag_circuit_breaker_trips_total", upstream=name) == 1.0  # fast-fails are not trips
    await asyncio.sleep(0.08)
    assert await call_upstream(b, fine, timeout_s=1, attempts=1) == "ok"
    assert sample("rag_circuit_breaker_open", upstream=name) == 0.0


# ------------------------------------------------------------------ readiness gauges
async def test_readyz_sets_rag_ready_and_per_dependency_gauges(make_app):
    class DeadRedis:
        async def ping(self):
            raise ConnectionError("redis gone")

    async with make_app() as e:
        assert (await e.client.get("/readyz")).status_code == 200
        assert sample("rag_ready") == 1.0
        for check in ("postgres", "redis", "qdrant", "embedding_config", "guardrails"):
            assert sample("rag_ready_check", check=check) == 1.0, check

        healthy = e.svc.redis
        e.svc.redis = DeadRedis()
        assert (await e.client.get("/readyz")).status_code == 503
        assert sample("rag_ready") == 0.0
        assert sample("rag_ready_check", check="redis") == 0.0 and sample("rag_ready_check", check="postgres") == 1.0

        e.svc.redis = healthy  # recovery flips it back
        assert (await e.client.get("/readyz")).status_code == 200
        assert sample("rag_ready") == 1.0 and sample("rag_ready_check", check="redis") == 1.0


# ------------------------------------------------------------------ ingestion queue depth + worker heartbeat
async def test_refresh_queue_metrics_reports_zcard_and_stamps_heartbeat():
    r, q = fakeredis.FakeAsyncRedis(), uniq("arq:queue:")
    before = time.time()
    assert await refresh_queue_metrics(r, q) == 0
    assert sample("rag_ingest_queue_depth") == 0.0
    assert sample("rag_worker_heartbeat_timestamp_seconds") >= before

    await r.zadd(q, {"job-a": 1, "job-b": 2, "job-c": 3})
    assert await refresh_queue_metrics(r, q) == 3 and sample("rag_ingest_queue_depth") == 3.0
    await r.zrem(q, "job-a")
    assert await refresh_queue_metrics(r, q) == 2 and sample("rag_ingest_queue_depth") == 2.0
    await r.aclose()


async def test_metrics_loop_survives_redis_errors_and_heartbeat_only_advances_on_success():
    class Flaky:
        def __init__(self):
            self.calls = 0

        async def zcard(self, key):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("redis blip")
            return 7

    r = Flaky()
    stale = time.time() - 3600
    from app.observability.metrics import WORKER_HEARTBEAT

    WORKER_HEARTBEAT.set(stale)
    task = asyncio.create_task(_metrics_loop(r, "arq:queue", 0.01))
    try:
        for _ in range(100):
            if sample("rag_ingest_queue_depth") == 7.0:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert r.calls >= 2  # the loop kept going after the failure
    assert sample("rag_ingest_queue_depth") == 7.0 and sample("rag_worker_heartbeat_timestamp_seconds") > stale + 3000


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("REDIS_INTEGRATION_URL"), reason="needs a real Redis: checks the arq queue key really is arq:queue")
async def test_queue_depth_counts_real_arq_enqueued_jobs():
    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(os.environ["REDIS_INTEGRATION_URL"]))
    try:
        base = await refresh_queue_metrics(pool)  # default queue name == arq's default
        job = await pool.enqueue_job("ingest_job", "q4-test-tenant", "/nonexistent", None, False, _defer_by=3600)
        assert job is not None
        assert await refresh_queue_metrics(pool) == base + 1
        assert sample("rag_ingest_queue_depth") == base + 1
    finally:
        if job is not None:
            await pool.zrem("arq:queue", job.job_id)
            await pool.delete(f"arq:job:{job.job_id}")
        await pool.aclose()


def test_worker_metrics_server_is_opt_out_and_never_fatal(monkeypatch):
    import app.worker as w

    monkeypatch.setattr(w, "_metrics_server_started", False)
    monkeypatch.setenv("WORKER_METRICS_PORT", "0")
    assert start_metrics_server() is False  # 0 disables
    monkeypatch.setenv("WORKER_METRICS_PORT", "not-a-port")
    assert start_metrics_server() is False  # garbage: warn, do not crash the worker

    def port_in_use(port, addr="0.0.0.0"):
        raise OSError("address already in use")

    monkeypatch.setenv("WORKER_METRICS_PORT", "9101")
    monkeypatch.setattr("app.metrics_server.start", port_in_use)  # e.g. a second worker on the same host
    assert start_metrics_server() is False  # OSError is swallowed: ingestion matters more than metrics
    assert w._metrics_server_started is False


def test_worker_metrics_server_serves_queue_gauges_over_http(monkeypatch):
    import app.worker as w

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handle = {}
    import app.metrics_server as ms

    real_start = ms.start
    monkeypatch.setattr(ms, "start", lambda p, addr="127.0.0.1": handle.setdefault("srv", real_start(p, addr=addr)))
    monkeypatch.setattr(w, "_metrics_server_started", False)
    monkeypatch.setenv("WORKER_METRICS_PORT", str(port))
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    try:
        assert start_metrics_server() is True and start_metrics_server() is True  # idempotent
        from app.observability.metrics import INGEST_QUEUE_DEPTH

        INGEST_QUEUE_DEPTH.set(5)
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
        assert "rag_ingest_queue_depth 5.0" in body and "rag_worker_heartbeat_timestamp_seconds" in body
    finally:
        srv = handle["srv"][0]
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ multiprocess gauge modes (what gunicorn + metrics_server do)
def _run_worker(mp_dir: Path, code: str) -> int:
    out = subprocess.run(
        [sys.executable, "-c", "import os\n" + code + "\nprint(os.getpid())"],
        env={**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(mp_dir)}, cwd=ROOT, capture_output=True, text=True, timeout=60, check=True,
    )
    return int(out.stdout.strip().splitlines()[-1])


def _merged(mp_dir: Path) -> CollectorRegistry:
    reg = CollectorRegistry()
    multiprocess.MultiProcessCollector(reg, path=str(mp_dir))
    return reg


def test_gauge_modes_across_workers(tmp_path):
    imp = "from app.observability.metrics import CIRCUIT_OPEN, READY, READY_CHECK, INGEST_QUEUE_DEPTH\n"
    a = _run_worker(tmp_path, imp + "CIRCUIT_OPEN.labels('llm').set(1); READY.set(1); READY_CHECK.labels('redis').set(1)")
    time.sleep(0.05)
    b = _run_worker(tmp_path, imp + "CIRCUIT_OPEN.labels('llm').set(1); READY.set(0); READY_CHECK.labels('redis').set(0)")
    _run_worker(tmp_path, imp + "CIRCUIT_OPEN.labels('llm').set(0)")  # worker C: breaker closed, never answered a /readyz probe

    reg = _merged(tmp_path)
    assert reg.get_sample_value("rag_circuit_breaker_open", {"upstream": "llm"}) == 2.0  # livesum: 2 of 3 workers have it open
    assert reg.get_sample_value("rag_ready") == 0.0  # livemostrecent: the latest probe (worker B) wins; worker C does not mask it
    assert reg.get_sample_value("rag_ready_check", {"check": "redis"}) == 0.0
    assert reg.get_sample_value("rag_ingest_queue_depth") is None  # never set anywhere -> not exported (no bogus 0)
    assert reg.get_sample_value("rag_worker_heartbeat_timestamp_seconds") is None

    multiprocess.mark_process_dead(a, str(tmp_path))  # what gunicorn's child_exit does for a recycled worker
    assert _merged(tmp_path).get_sample_value("rag_circuit_breaker_open", {"upstream": "llm"}) == 1.0
    multiprocess.mark_process_dead(b, str(tmp_path))
    reg = _merged(tmp_path)
    assert reg.get_sample_value("rag_ready") is None  # no live worker has answered a probe any more


def test_queue_gauges_exported_by_worker_process_in_multiprocess_mode(tmp_path):
    _run_worker(tmp_path, "from app.observability.metrics import INGEST_QUEUE_DEPTH, WORKER_HEARTBEAT\nINGEST_QUEUE_DEPTH.set(4); WORKER_HEARTBEAT.set_to_current_time()")
    reg = _merged(tmp_path)
    assert reg.get_sample_value("rag_ingest_queue_depth") == 4.0
    assert reg.get_sample_value("rag_worker_heartbeat_timestamp_seconds") > 1.6e9


# ------------------------------------------------------------------ names the alert rules / dashboard query
async def test_instrumentator_metric_names_and_grouped_status_labels(make_app, new_key, monkeypatch):
    built = {}
    harness = make_app.__wrapped__.__globals__  # the module that defines the make_app fixture (pytest may import it under another name)
    real_create_app = harness["create_app"]
    monkeypatch.setitem(harness, "create_app", lambda *a, **kw: built.setdefault("app", real_create_app(*a, **kw)))
    _, k = new_key()
    async with make_app() as e:
        assert (await e.client.post("/query", json={"q": "hello", "session_id": "s1"}, headers=H(k))).status_code == 200
        assert (await e.client.post("/query", json={"q": "hello", "session_id": "s1"}, headers=H("rag_bad"))).status_code == 401
        e.svc.graph.ainvoke = AsyncMock(side_effect=ValueError("kaboom"))
        assert (await e.client.post("/query", json={"q": "hello", "session_id": "s2"}, headers=H(k))).status_code == 500
        await e.client.get("/healthz")
        await e.client.get("/readyz")
        text = generate_latest(built["app"].state.metrics_registry).decode()
    # status is GROUPED (2xx/4xx/5xx): a rule written as status="500" or status=~"5\d\d" would silently match nothing
    assert 'http_requests_total{handler="/query",method="POST",status="2xx"}' in text
    assert 'http_requests_total{handler="/query",method="POST",status="4xx"}' in text
    assert 'http_requests_total{handler="/query",method="POST",status="5xx"}' in text
    assert "/healthz" not in text and "/readyz" not in text  # probes are excluded from the HTTP metrics
    # the p95/p99 rules use the unlabelled high-resolution histogram; the per-handler one only has 0.1/0.5/1 s buckets
    assert 'http_request_duration_highr_seconds_bucket{le="5.0"}' in text and 'http_request_duration_highr_seconds_bucket{le="10.0"}' in text
    assert 'http_request_duration_seconds_bucket{handler="/query",le="1.0",method="POST"}' in text
    assert 'le="2.0",method="POST"' not in text


def test_rate_limit_windows_are_exported_from_process_start():
    for window in ("minute", "day", "fallback", "auth_fail"):
        assert sample("rag_rate_limited_total", window=window) is not None, window


# ------------------------------------------------------------------ semantics the runbooks depend on (characterisation)
class _DeadPipeline:
    def pipeline(self, *a, **kw):
        raise ConnectionError("redis down")


async def test_limiter_fallback_counter_counts_rejections_only_not_activations():
    """rag_limiter_fallback_total moves only when the in-process fallback REJECTS a request. Serving requests under the
    fallback (Redis down) is invisible in that counter - the RagLimiterFallbackActive rule therefore also watches
    rag_ready_check{check="redis"}."""
    lim = RateLimiter(_DeadPipeline(), workers=1)
    p = Principal(key_id=uniq("k"), tenant_id="t", user_id="u", is_admin=False, rate_per_min=2, rate_per_day=1000)
    before = sample("rag_limiter_fallback_total")
    await lim.check(p)
    await lim.check(p)  # both allowed by the fallback bucket
    assert sample("rag_limiter_fallback_total") == before
    from app.ratelimit import Limited

    with pytest.raises(Limited):
        await lim.check(p)
    assert sample("rag_limiter_fallback_total") == before + 1


def _client_from(e, ip):
    tr = httpx.ASGITransport(app=e.client._transport.app, client=(ip, 5000), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=tr, base_url="http://t")


@pytest.mark.xfail(
    reason="KNOWN GAP (reported, outside Q4 file ownership): rag_rate_limited_total{window='auth_fail'} is only incremented when two "
    "failing requests race past AuthFailLimiter.exceeded(); the penalty-box 429s (the normal case) are not counted. "
    "RagAuthFailure429 undercounts until app/api.py authenticate() or app/auth/guard.py counts them.",
    strict=False,
)
async def test_auth_fail_429_counter_counts_penalty_box_responses(make_app):
    ip = f"198.51.{random.randint(1, 250)}.{random.randint(1, 250)}"
    async with make_app(auth_fail_per_min=3) as e:
        c = _client_from(e, ip)
        before = sample("rag_rate_limited_total", window="auth_fail")
        codes = [(await c.post("/query", json={"q": "hi", "session_id": "s"}, headers=H(f"rag_{uuid.uuid4().hex}"))).status_code for _ in range(6)]
        await c.aclose()
    assert codes == [401, 401, 401, 429, 429, 429]
    assert sample("rag_rate_limited_total", window="auth_fail") - before == 3
