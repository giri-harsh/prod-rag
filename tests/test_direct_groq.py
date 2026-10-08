"""USE_GATEWAY=false: direct Groq with in-code primary->fallback key failover (same timeouts / retries / breaker as the gateway path)."""
import asyncio
import sys
import types

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.config import ConfigError, load_settings
from app.errors import CircuitOpen
from app.guardrails.rails import FailoverChat
from app.observability.metrics import LLM_FAILOVER
from app.resilience import Breakers
from app.services.llm import GroqDirectLLM, PortkeyLLM, failover_worthy, make_llm


class StatusErr(Exception):
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.status_code = code


def make_fake_groq(monkeypatch, scripts):
    """scripts: {key: [outcome, ...]} where an outcome is an Exception or a text reply. Records every create() call."""
    calls = []

    class Msg:
        def __init__(self, c):
            self.content = c

    class Choice:
        def __init__(self, c):
            self.message, self.finish_reason = Msg(c), "stop" if c else "length"

    class Resp:
        def __init__(self, c):
            self.choices = [Choice(c)]
            self.usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=2)

    class Completions:
        def __init__(self, key):
            self.key = key

        async def create(self, **kw):
            calls.append((self.key, kw))
            outs = scripts[self.key]
            o = outs.pop(0) if len(outs) > 1 else outs[0]
            if isinstance(o, Exception):
                raise o
            return Resp(o)

    class AsyncGroq:
        def __init__(self, api_key, timeout, max_retries):
            assert max_retries == 0 and timeout > 0
            self.chat = types.SimpleNamespace(completions=Completions(api_key))

    mod = types.ModuleType("groq")
    mod.AsyncGroq = AsyncGroq
    monkeypatch.setitem(sys.modules, "groq", mod)
    return calls


def settings(**kw):
    base = dict(use_gateway=False, groq_api_key="PRIMARY", groq_fallback_api_key="FALLBACK", llm_primary_model="openai/gpt-oss-120b",
                llm_fallback_model="openai/gpt-oss-20b", max_attempts=3, database_url="postgresql://x", redis_url="redis://x", qdrant_url=":memory:")
    base.update(kw)
    return load_settings(_env_file=None, **base)


async def test_primary_key_and_model_used_with_reasoning_effort(monkeypatch):
    calls = make_fake_groq(monkeypatch, {"PRIMARY": ["hello"], "FALLBACK": ["unused"]})
    llm = GroqDirectLLM(settings(), Breakers(5, 30))
    r = await llm.complete("sys", "usr", feature="t")
    assert r.text == "hello" and r.prompt_tokens == 5
    (key, kw), = calls
    assert key == "PRIMARY" and kw["model"] == "openai/gpt-oss-120b" and kw["reasoning_effort"] == "low" and kw["max_completion_tokens"] == 2048
    await llm.complete("sys", "usr", feature="t", fast=True)
    assert calls[-1][1]["model"] == "openai/gpt-oss-20b"  # fast=True => the cheap model (query rewriting)


async def test_non_gpt_oss_models_get_no_reasoning_param(monkeypatch):
    calls = make_fake_groq(monkeypatch, {"PRIMARY": ["x"], "FALLBACK": ["x"]})
    await GroqDirectLLM(settings(llm_primary_model="llama-3.3-70b-versatile"), Breakers(5, 30)).complete("s", "u", feature="t")
    assert "reasoning_effort" not in calls[0][1]


@pytest.mark.parametrize("err", [StatusErr(429), StatusErr(401), StatusErr(503), asyncio.TimeoutError(), ConnectionError("reset")])
async def test_failover_to_the_fallback_key(monkeypatch, err):
    calls = make_fake_groq(monkeypatch, {"PRIMARY": [err], "FALLBACK": ["from fallback"]})
    before = sum(s.value for m in LLM_FAILOVER.collect() for s in m.samples if s.name.endswith("_total"))
    r = await GroqDirectLLM(settings(), Breakers(5, 30)).complete("s", "u", feature="t")
    assert r.text == "from fallback" and [c[0] for c in calls] == ["PRIMARY", "FALLBACK"]
    assert sum(s.value for m in LLM_FAILOVER.collect() for s in m.samples if s.name.endswith("_total")) == before + 1


async def test_bad_request_does_not_burn_the_fallback_key(monkeypatch):
    calls = make_fake_groq(monkeypatch, {"PRIMARY": [StatusErr(400)], "FALLBACK": ["x"]})
    with pytest.raises(StatusErr):
        await GroqDirectLLM(settings(), Breakers(5, 30)).complete("s", "u", feature="t")
    assert [c[0] for c in calls] == ["PRIMARY"]  # 400 would fail identically on the other key
    assert not failover_worthy(StatusErr(400)) and failover_worthy(StatusErr(429)) and not failover_worthy(ValueError("x"))


async def test_retries_with_backoff_then_breaker_opens(monkeypatch):
    calls = make_fake_groq(monkeypatch, {"PRIMARY": [StatusErr(503)], "FALLBACK": [StatusErr(503)]})
    br = Breakers(2, 30)  # opens after 2 failed CALLS
    llm = GroqDirectLLM(settings(), br)
    for _ in range(2):
        with pytest.raises(StatusErr):
            await llm.complete("s", "u", feature="t")
    # each call = 3 tenacity attempts x (primary + fallback)
    assert len(calls) == 2 * 3 * 2
    n = len(calls)
    with pytest.raises(CircuitOpen):
        await llm.complete("s", "u", feature="t")
    assert len(calls) == n  # open circuit: Groq is not called at all


async def test_single_key_still_works_and_errors_propagate(monkeypatch):
    calls = make_fake_groq(monkeypatch, {"PRIMARY": [StatusErr(429)]})
    llm = GroqDirectLLM(settings(groq_fallback_api_key=None, max_attempts=1), Breakers(5, 30))
    with pytest.raises(StatusErr):
        await llm.complete("s", "u", feature="t")
    assert len(calls) == 1


async def test_empty_reasoning_only_completion_is_an_error(monkeypatch):
    make_fake_groq(monkeypatch, {"PRIMARY": [""], "FALLBACK": [""]})
    with pytest.raises(ValueError, match="empty completion"):
        await GroqDirectLLM(settings(), Breakers(5, 30)).complete("s", "u", feature="t")


def test_make_llm_switches_on_use_gateway(monkeypatch):
    make_fake_groq(monkeypatch, {"PRIMARY": ["x"], "FALLBACK": ["x"]})
    pk = types.ModuleType("portkey_ai")
    pk.AsyncPortkey = lambda **kw: types.SimpleNamespace(chat=None)
    monkeypatch.setitem(sys.modules, "portkey_ai", pk)
    assert isinstance(make_llm(settings(use_gateway=False), Breakers(5, 30)), GroqDirectLLM)
    assert isinstance(make_llm(settings(use_gateway=True, portkey_api_key="PK"), Breakers(5, 30)), PortkeyLLM)


def test_prod_requires_portkey_only_when_the_gateway_is_used():
    base = dict(app_env="prod", groq_api_key="g", gemini_api_key="m", database_url="postgresql://x", redis_url="redis://x", qdrant_url="http://q:6333", _env_file=None)
    assert load_settings(use_gateway=False, **base).use_gateway is False
    with pytest.raises(ConfigError, match="PORTKEY_API_KEY"):
        load_settings(use_gateway=True, **base)
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        load_settings(use_gateway=False, **{**base, "groq_api_key": None})


async def test_guard_llm_fails_over_between_keys():
    class Boom(FakeListChatModel):
        async def _agenerate(self, *a, **k):
            raise StatusErr(429)

        def _generate(self, *a, **k):
            raise StatusErr(429)

    good = FakeListChatModel(responses=["No"])
    assert (await FailoverChat(models=[Boom(responses=["x"]), good]).ainvoke("hi")).content == "No"
    assert FailoverChat(models=[Boom(responses=["x"]), good]).invoke("hi").content == "No"
    with pytest.raises(StatusErr):  # every key failing => error => the rails fail closed (503) upstream
        await FailoverChat(models=[Boom(responses=["x"]), Boom(responses=["y"])]).ainvoke("hi")
