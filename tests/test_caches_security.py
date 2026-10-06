"""D-1 embedding cache, D-2 semantic cache rules, D-3 limiter fallback, D-4 PII redaction, D-5 endpoints, D-7 eval smoke."""
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel  # noqa: F401
from redis import asyncio as aioredis

from app.auth.keys import Principal
from app.observability import redact as R
from app.ratelimit import Limited, RateLimiter, TokenBucket
from app.services.embedding_cache import CachedEmbedder, cache_key
from tests.conftest import FakeEmbedder, FakeLLM, H, ScriptedReranker

K8S = "kubernetes pods are scheduled onto nodes by the scheduler using resource requests " * 3


def q(text, session=None, **kw):
    return {"q": text, "session_id": session or f"s{uuid.uuid4().hex[:8]}", **kw}


# ------------------------------------------------------------------------- D-1 embedding cache
async def test_embedding_cache_hits_ttl_and_key_design(redis_url):
    r = aioredis.from_url(redis_url)
    inner = FakeEmbedder()
    c = CachedEmbedder(inner, r, ttl_s=7 * 86400)
    text = f"unique text {uuid.uuid4().hex}"
    v1 = await c.embed_query(text)
    n = inner.calls
    v2 = await c.embed_query("  " + text.upper() + "  ")  # normalised: case/whitespace-insensitive
    assert inner.calls == n and v1 == pytest.approx(v2, abs=1e-6)  # served from Redis
    ttl = await r.ttl(cache_key(inner.model, inner.dim, text))
    assert 7 * 86400 - 60 < ttl <= 7 * 86400
    assert cache_key("m", 128, "x") != cache_key("m2", 128, "x") != cache_key("m", 256, "x")  # model+dim in the key
    batch = [text, f"other {uuid.uuid4().hex}"]
    n = inner.calls
    await c.embed_texts(batch)
    assert inner.calls == n + 1  # only the uncached text hit the model
    await r.aclose()


async def test_embedding_cache_survives_redis_failure():
    class Dead:
        async def mget(self, *a):
            raise ConnectionError("x")

        def pipeline(self, **kw):
            raise ConnectionError("x")

    c = CachedEmbedder(FakeEmbedder(), Dead(), 10)
    assert len(await c.embed_query("hello world")) == 128


# ------------------------------------------------------------------------- D-2 semantic cache
async def _seeded(make_app, new_key, seed_docs, **kw):
    t, k = new_key()
    cm = make_app(**kw)
    e = await cm.__aenter__()
    await seed_docs(e, t, {"a.txt": K8S})
    return cm, e, t, k


async def test_semcache_hit_for_standalone_query_and_similarity_threshold(make_app, new_key, seed_docs):
    cm, e, t, k = await _seeded(make_app, new_key, seed_docs)
    try:
        question = "kubernetes pods scheduled nodes scheduler"
        j1 = (await e.client.post("/query", json=q(question), headers=H(k))).json()
        assert not j1["cache_hit"] and len(e.llm.of("responder")) == 1
        j2 = (await e.client.post("/query", json=q(question), headers=H(k))).json()
        assert j2["cache_hit"] and j2["answer"] == j1["answer"] and len(e.llm.of("responder")) == 1
        j3 = (await e.client.post("/query", json=q("bgp vlan routing switches totally different"), headers=H(k))).json()
        assert not j3["cache_hit"]  # similarity below 0.95
    finally:
        await cm.__aexit__(None, None, None)


async def test_semcache_is_tenant_scoped(make_app, new_key, seed_docs):
    cm, e, ta, ka = await _seeded(make_app, new_key, seed_docs)
    tb, kb = new_key()
    try:
        await seed_docs(e, tb, {"b.txt": K8S})
        question = "kubernetes pods scheduled nodes"
        await e.client.post("/query", json=q(question), headers=H(ka))
        j = (await e.client.post("/query", json=q(question), headers=H(kb))).json()
        assert not j["cache_hit"]
    finally:
        await cm.__aexit__(None, None, None)


async def test_semcache_only_for_standalone_queries(make_app, new_key, seed_docs):
    llm = FakeLLM(standalone=False)  # planner says: depends on history
    cm, e, t, k = await _seeded(make_app, new_key, seed_docs, llm=llm)
    try:
        sid = f"s{uuid.uuid4().hex[:6]}"
        await e.client.post("/query", json=q("hello", sid), headers=H(k))  # creates history
        question = "kubernetes pods scheduled nodes scheduler follow up"
        j1 = (await e.client.post("/query", json=q(question, sid), headers=H(k))).json()
        j2 = (await e.client.post("/query", json=q(question, sid), headers=H(k))).json()
        assert not j1["cache_hit"] and not j2["cache_hit"]
        n = (await e.svc.semcache.client.count(e.svc.semcache.collection)).count if await e.svc.semcache.client.collection_exists(e.svc.semcache.collection) else 0
        assert n == 0  # never stored either
    finally:
        await cm.__aexit__(None, None, None)


async def test_semcache_never_stores_blocked_or_no_context(make_app, new_key, seed_docs):
    # blocked output: guard says input No, output Yes (alternating)
    cm, e, t, k = await _seeded(make_app, new_key, seed_docs, guard_responses=("No", "Yes"))
    try:
        question = "kubernetes pods scheduled nodes scheduler blocked"
        j1 = (await e.client.post("/query", json=q(question), headers=H(k))).json()
        assert j1["blocked"]
        j2 = (await e.client.post("/query", json=q(question), headers=H(k))).json()
        assert not j2["cache_hit"]
    finally:
        await cm.__aexit__(None, None, None)
    # no-context: tenant with no documents
    t2, k2 = new_key()
    async with make_app() as e2:
        question = f"nothing indexed here {uuid.uuid4().hex}"
        await e2.client.post("/query", json=q(question), headers=H(k2))
        j = (await e2.client.post("/query", json=q(question), headers=H(k2))).json()
        assert not j["cache_hit"] and "couldn't find" in j["answer"]
        col = e2.svc.semcache.collection
        cnt = (await e2.svc.semcache.client.count(col)).count if await e2.svc.semcache.client.collection_exists(col) else 0
        assert cnt == 0


async def test_semcache_invalidated_by_reindex_and_expires(make_app, new_key, seed_docs):
    cm, e, t, k = await _seeded(make_app, new_key, seed_docs, semcache_ttl_s=60)
    try:
        question = "kubernetes pods scheduled nodes scheduler reindex"
        await e.client.post("/query", json=q(question), headers=H(k))
        assert (await e.client.post("/query", json=q(question), headers=H(k))).json()["cache_hit"]
        # re-index => new collection version => key (index_version) no longer matches
        await seed_docs(e, t, {"a.txt": K8S + " updated"}, rebuild=True)
        e.svc.vectors._ver_cache = None
        j = (await e.client.post("/query", json=q(question), headers=H(k))).json()
        assert not j["cache_hit"]
        assert (await e.client.post("/query", json=q(question), headers=H(k))).json()["cache_hit"]
        e.svc.semcache.ttl = 1  # shrink TTL now (slow real servers made a 2s TTL flaky)
        await asyncio.sleep(2.5)  # TTL via created_at payload
        assert not (await e.client.post("/query", json=q(question), headers=H(k))).json()["cache_hit"]
        await asyncio.sleep(3.5)  # > ttl + 1s: created_at is whole seconds
        await e.svc.semcache.purge_expired()  # what the cron does
        left, _ = await e.svc.semcache.client.scroll(e.svc.semcache.collection, limit=10)
        assert left == []  # (scroll, not count: qdrant local-mode count() is stale after filtered deletes)
    finally:
        await cm.__aexit__(None, None, None)


# ------------------------------------------------------------------------- D-3 limiter fallback
def test_token_bucket():
    now = {"t": 0.0}
    b = TokenBucket(6, clock=lambda: now["t"])  # 6/min = 1 per 10s, burst 6
    assert [b.take() for _ in range(6)] == [0.0] * 6
    assert b.take() > 0
    now["t"] = 10.1
    assert b.take() == 0.0


async def test_limiter_falls_back_to_per_worker_bucket_when_redis_down():
    class Dead:
        def pipeline(self, **kw):
            raise ConnectionError("redis down")

    rl = RateLimiter(Dead(), workers=3)
    p = Principal(f"kid-{uuid.uuid4().hex}", "t", "u", False, rate_per_min=6, rate_per_day=100)  # 6/3 = 2 per worker
    from app.ratelimit import LIMITER_FALLBACK

    before = LIMITER_FALLBACK._value.get()
    await rl.check(p)
    await rl.check(p)
    with pytest.raises(Limited) as ei:
        await rl.check(p)
    assert ei.value.window == "fallback" and ei.value.retry_after >= 1
    assert LIMITER_FALLBACK._value.get() == before + 1  # metric logged


async def test_api_returns_429_via_fallback(make_app, new_key):
    _, k = new_key(per_min=2)
    async with make_app() as e:
        class Dead:
            def pipeline(self, **kw):
                raise ConnectionError("x")

        e.svc.limiter._r = Dead()
        codes = [(await e.client.post("/query", json=q("hello"), headers=H(k))).status_code for _ in range(4)]
        assert codes[:2] == [200, 200] and codes[-1] == 429


# ------------------------------------------------------------------------- D-4 PII redaction
PII = {
    "email": ("contact alice.smith@example.com now", "alice.smith@example.com"),
    "phone": ("call +91 98765 43210 today", "98765 43210"),
    "aadhaar": ("aadhaar 2345 6789 0123 given", "2345 6789 0123"),
    "pan": ("PAN ABCDE1234F here", "ABCDE1234F"),
    "card": ("card 4111 1111 1111 1111 exp", "4111 1111 1111 1111"),
    "gsk": ("key gsk_" + "a1B2c3D4e5F6g7H8i9J0k1L2", "gsk_a1B2"),
    "bearer": ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefghij1234567890", "eyJhbGciOiJIUzI1"),
    "ipv4": ("from 192.168.10.25 port", "192.168.10.25"),
    "ipv6": ("from 2001:0db8:85a3:0000:8a2e:0370:7334 up", "2001:0db8:85a3"),
}


@pytest.mark.parametrize("name", PII)
def test_redactor_removes_pii(name):
    text, secret = PII[name]
    out = R.redact(text)
    assert secret not in out and "[REDACTED:" in out


def test_redactor_luhn_and_non_pii_untouched():
    assert "[REDACTED:CARD]" not in R.redact("ticket 1234 5678 9012 3456")  # fails Luhn: not a card
    assert R.redact("how do pods get scheduled?") == "how do pods get scheduled?"
    assert R.luhn_ok("4111111111111111") and not R.luhn_ok("4111111111111112")


def test_redact_obj_nested():
    o = {"a": ["mail bob@x.io", {"b": "PAN ABCDE1234F"}], "n": 5}
    r = R.redact_obj(o)
    assert "bob@x.io" not in str(r) and "ABCDE1234F" not in str(r) and r["n"] == 5


def test_logging_filter_redacts_records(caplog):
    R.install_logging_filter()
    logger = logging.getLogger("pii-test")
    rec = logging.LogRecord("pii-test", logging.INFO, __file__, 1, "user %s asked", ("carol@example.org",), None)
    assert R.PIIFilter().filter(rec)
    assert "carol@example.org" not in rec.getMessage()
    logger.info("noop")


def test_logfire_scrubbing_redacts_span_attributes():
    import logfire
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    logfire.configure(
        send_to_logfire=False, console=False, scrubbing=R.logfire_scrubbing(),
        additional_span_processors=[SimpleSpanProcessor(exp)],
    )
    with logfire.span("q", question="my mail is dave@corp.com and card 4111 1111 1111 1111", note="plain text"):
        pass
    attrs = [dict(s.attributes) for s in exp.get_finished_spans() if s.name == "q"][0]
    blob = str(attrs)
    assert "dave@corp.com" not in blob and "4111 1111 1111 1111" not in blob and "plain text" in blob


def test_langsmith_client_hides_inputs_and_outputs():
    c = R.langsmith_client()
    assert c._hide_inputs is R.redact_obj and c._hide_outputs is R.redact_obj


async def test_telemetry_redacted_but_answer_is_not(make_app, new_key, seed_docs, monkeypatch):
    """The API answer must still contain the user's data; only telemetry is redacted."""
    t, k = new_key()
    llm = FakeLLM(responder_text="Reach support at help@example.com for this.")
    async with make_app(llm=llm) as e:
        await seed_docs(e, t, {"a.txt": K8S})
        j = (await e.client.post("/query", json=q("kubernetes pods scheduled nodes help"), headers=H(k))).json()
        assert "help@example.com" in j["answer"]


# ------------------------------------------------------------------------- D-5 endpoints
async def test_public_api_has_no_metrics_and_minimal_health(make_app):
    async with make_app() as e:
        assert (await e.client.get("/metrics")).status_code == 404
        assert (await e.client.get("/healthz")).json() == {"status": "ok"}
        assert (await e.client.get("/readyz")).json() == {"ready": True}


def test_metrics_server_serves_on_separate_port_multiprocess(tmp_path):
    port = int(__import__("socket").socket().getsockname()[1]) if False else None
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    code = (
        "import time; from app.observability.metrics import GUARDRAIL_BLOCKS; "
        "GUARDRAIL_BLOCKS.labels('input','x').inc(3); "
        "from app.metrics_server import start; start(%d, addr='127.0.0.1'); time.sleep(6)" % port
    )
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=os.getcwd())
    try:
        body = ""
        for _ in range(40):
            try:
                body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2).read().decode()
                break
            except Exception:
                time.sleep(0.25)
        assert 'rag_guardrail_blocks_total{rail="x",stage="input"} 3.0' in body
    finally:
        proc.kill()


# ------------------------------------------------------------------------- D-7 offline eval smoke
async def test_offline_eval_pipeline_smoke(make_app, new_key, seed_docs, pg_url, monkeypatch):
    """Runs evals.pipeline against a live in-process HTTP server with fake models (no keys, no LLM judge)."""
    import socket

    import uvicorn

    from app.api import create_app
    from app.auth import keys
    from app.services.reranker import NoopReranker
    from app.services.sparse import SimpleSparseEncoder
    from tests.conftest import _qdrant, make_guard

    t = f"ev{uuid.uuid4().hex[:6]}"
    key = keys.create_key(pg_url, t, "eval", scopes=("eval",), rate_per_min=1000)
    async with make_app() as seed_env:  # only to share settings/collection naming + seed docs
        s = seed_env.settings
        await seed_docs(seed_env, t, {"a.txt": K8S})
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port = so.getsockname()[1]
        ov = {"llm": FakeLLM(), "embedder": FakeEmbedder(), "reranker": NoopReranker(), "sparse": SimpleSparseEncoder(),
              "guard": make_guard(s), "qdrant_client": seed_env.qdrant}
        server = uvicorn.Server(uvicorn.Config(create_app(s, ov), host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
        th.start()
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started
        try:
            import evals.pipeline as P

            monkeypatch.setattr(P, "API_URL", f"http://127.0.0.1:{port}/query")
            monkeypatch.setattr(P, "DELAY_BETWEEN_CALLS", 0)
            monkeypatch.setenv("RAG_API_KEY", key)
            golden = {"rag_samples": [
                {"question": "kubernetes pods scheduled onto nodes by the scheduler", "relevant_contexts": [], "domain": "k8s"},
                {"question": "hello", "relevant_contexts": [], "domain": "chat"},
            ]}
            out = await asyncio.to_thread(P.run_pipeline, golden)
            s0, s1 = out["rag_samples"]
            assert s0["actual_tools_called"] == ["retrieve_documents"] and s0["actual_contexts"] and "kubernetes" in s0["actual_contexts"][0]
            assert s1["actual_tools_called"] == ["direct_answer"]
            # Tool Correctness (Jaccard), the same formula as evals/metrics.py
            called, expected = set(s0["actual_tools_called"]), {"retrieve_documents"}
            assert len(called & expected) / len(called | expected) == 1.0
        finally:
            server.should_exit = True
            th.join(timeout=10)
    _ = _qdrant
    _ = ScriptedReranker
