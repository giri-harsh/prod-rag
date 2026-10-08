"""P1-3 timeouts/retries/breaker, P1-4 embeddings, P1-2 LLM concurrency bound, P0-3 config validation."""
import asyncio
import sys
import types

import pytest

from app.config import ConfigError, load_settings
from app.errors import CircuitOpen
from app.resilience import Breakers, CircuitBreaker, call_upstream

FAST = dict(wait_max_s=0.01)


class HTTPErr(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status_code = status


async def test_retries_429_5xx_and_timeouts_up_to_3_attempts():
    n = {"c": 0}

    async def flaky():
        n["c"] += 1
        if n["c"] < 3:
            raise HTTPErr(429 if n["c"] == 1 else 503)
        return "ok"

    assert await call_upstream(CircuitBreaker("t"), flaky, timeout_s=1, attempts=3, **FAST) == "ok"
    assert n["c"] == 3

    n["c"] = 0

    async def always():
        n["c"] += 1
        raise HTTPErr(500)

    with pytest.raises(HTTPErr):
        await call_upstream(CircuitBreaker("t"), always, timeout_s=1, attempts=3, **FAST)
    assert n["c"] == 3  # max 3 attempts


async def test_non_retryable_errors_are_not_retried():
    n = {"c": 0}

    async def bad():
        n["c"] += 1
        raise HTTPErr(400)

    with pytest.raises(HTTPErr):
        await call_upstream(CircuitBreaker("t"), bad, timeout_s=1, attempts=3, **FAST)
    assert n["c"] == 1


async def test_timeout_triggers_and_is_retried():
    n = {"c": 0}

    async def slow():
        n["c"] += 1
        await asyncio.sleep(1)

    with pytest.raises(asyncio.TimeoutError):
        await call_upstream(CircuitBreaker("t"), slow, timeout_s=0.05, attempts=3, **FAST)
    assert n["c"] == 3


async def test_circuit_breaker_opens_half_opens_and_closes():
    now = {"t": 0.0}
    br = CircuitBreaker("x", failures=5, reset_s=30, clock=lambda: now["t"])
    calls = {"c": 0}

    async def fail():
        calls["c"] += 1
        raise HTTPErr(400)

    for _ in range(5):
        with pytest.raises(HTTPErr):
            await call_upstream(br, fail, timeout_s=1, attempts=1)
    assert br.state == "open"
    with pytest.raises(CircuitOpen):  # short-circuits: upstream not called
        await call_upstream(br, fail, timeout_s=1, attempts=1)
    assert calls["c"] == 5

    now["t"] = 31  # half-open: one probe allowed; a failed probe re-opens
    assert br.state == "half-open"
    with pytest.raises(HTTPErr):
        await call_upstream(br, fail, timeout_s=1, attempts=1)
    assert br.state == "open"

    now["t"] = 70

    async def ok():
        return 1

    assert await call_upstream(br, ok, timeout_s=1, attempts=1) == 1
    assert br.state == "closed"


# ---------------------------------------------------------------- embeddings (P1-4)
def _fake_genai(monkeypatch, client_cls):
    mod = types.ModuleType("langchain_google_genai")
    mod.GoogleGenerativeAIEmbeddings = client_cls
    monkeypatch.setitem(sys.modules, "langchain_google_genai", mod)


async def test_embed_query_retries_pins_dimension_and_normalizes(monkeypatch, settings):
    seen = {}

    class Client:
        n = 0

        def __init__(self, **kw):
            seen.update(kw)

        async def aembed_query(self, text):
            Client.n += 1
            if Client.n < 3:
                raise HTTPErr(429)
            return [3.0, 4.0] + [0.0] * 126

        async def aembed_documents(self, texts):
            return [[1.0] + [0.0] * 127 for _ in texts]

    _fake_genai(monkeypatch, Client)
    from app.services.embedding import GeminiEmbedder

    s = settings(embedding_model="gemini-embedding-2", embedding_dim=128, max_attempts=3)
    emb = GeminiEmbedder(s, Breakers(5, 30))
    assert seen["model"] == "models/gemini-embedding-2" and seen["output_dimensionality"] == 128
    v = await emb.embed_query("x")
    assert Client.n == 3 and abs(v[0] - 0.6) < 1e-9 and abs(v[1] - 0.8) < 1e-9
    assert len(await emb.embed_texts(["a"] * 250)) == 250  # batched (100/call)


async def test_embedder_hard_fails_on_wrong_dimension(monkeypatch, settings):
    class Client:
        def __init__(self, **kw): ...
        async def aembed_query(self, t):
            return [1.0] * 64  # wrong size: no silent fallback, must raise

    _fake_genai(monkeypatch, Client)
    from app.services.embedding import GeminiEmbedder

    emb = GeminiEmbedder(settings(embedding_dim=128), Breakers(5, 30))
    with pytest.raises(ValueError, match="dim"):
        await emb.embed_query("x")


def test_no_local_embedding_fallback_in_code():
    import pathlib

    src = pathlib.Path("app/services/embedding.py").read_text()
    assert "sentence_transformers" not in src and "SentenceTransformer" not in src


# ---------------------------------------------------------------- LLM client
async def test_portkey_llm_retries_bounds_concurrency_and_counts_tokens(monkeypatch, settings):
    state = {"inflight": 0, "max": 0, "fail_once": True}

    class Usage:
        prompt_tokens, completion_tokens = 7, 3

    class Msg:
        content = "hi"

    class Resp:
        usage = Usage()
        choices = [type("C", (), {"message": Msg()})()]

    class Completions:
        async def create(self, **kw):
            if state["fail_once"]:
                state["fail_once"] = False
                raise HTTPErr(503)
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.02)
            state["inflight"] -= 1
            return Resp()

    class FakePortkey:
        def __init__(self, **kw):
            state.setdefault("configs", []).append(kw["config"])
            self.chat = type("Chat", (), {"completions": Completions()})()

    mod = types.ModuleType("portkey_ai")
    mod.AsyncPortkey = FakePortkey
    monkeypatch.setitem(sys.modules, "portkey_ai", mod)
    from app.observability.metrics import LLM_TOKENS
    from app.services.llm import PortkeyLLM

    llm = PortkeyLLM(settings(llm_concurrency=2), Breakers(5, 30))
    res = await asyncio.gather(*[llm.complete("s", "u", feature="t") for _ in range(6)])
    assert all(r.text == "hi" and r.prompt_tokens == 7 for r in res)
    assert state["max"] <= 2  # semaphore bound
    cfg, fast_cfg = state["configs"]
    assert fast_cfg["strategy"]["mode"] == "single" and len(fast_cfg["targets"]) == 1  # cheap rewrite model
    assert cfg["strategy"]["mode"] == "fallback" and len(cfg["targets"]) == 2 and cfg["request_timeout"] > 0
    assert LLM_TOKENS.labels("t", "prompt")._value.get() >= 42


# ---------------------------------------------------------------- config validation (P0-3)
def test_config_fails_fast_with_clear_messages(monkeypatch):
    for k in ("DATABASE_URL", "REDIS_URL", "QDRANT_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError) as ei:
        load_settings(_env_file=None)
    msg = str(ei.value)
    assert "DATABASE_URL" in msg and "REDIS_URL" in msg and "QDRANT_URL" in msg


def test_config_prod_requires_keys_and_valid_urls():
    base = dict(database_url="postgresql://x", redis_url="redis://x", qdrant_url="http://q:6333", _env_file=None)
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        load_settings(app_env="prod", **base)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings(**{**base, "database_url": "mysql://x"})
    with pytest.raises(ConfigError, match="REDIS_URL"):
        load_settings(**{**base, "redis_url": "http://x"})
    ok = load_settings(app_env="prod", groq_api_key="a", portkey_api_key="b", gemini_api_key="c", **base)
    assert ok.embedding_model == "gemini-embedding-2" and ok.llm_timeout_s == 30 and ok.embed_timeout_s == 10 and ok.qdrant_timeout_s == 5


async def test_cancelled_half_open_probe_does_not_wedge_the_breaker():
    now = {"t": 0.0}
    br = CircuitBreaker("x", failures=1, reset_s=10, clock=lambda: now["t"])

    async def fail():
        raise HTTPErr(400)

    with pytest.raises(HTTPErr):
        await call_upstream(br, fail, timeout_s=1, attempts=1)
    now["t"] = 11  # half-open: the next call is the single probe
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(call_upstream(br, hang, timeout_s=30, attempts=1))
    await started.wait()
    task.cancel()  # e.g. the client disconnected mid-probe
    with pytest.raises(asyncio.CancelledError):
        await task

    async def ok():
        return 1

    assert await call_upstream(br, ok, timeout_s=1, attempts=1) == 1  # a new probe is allowed
    assert br.state == "closed"
