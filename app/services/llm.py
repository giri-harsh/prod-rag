"""Async chat LLM for Groq models: via the Portkey gateway (USE_GATEWAY=true) or directly with in-code key failover (false).
Both paths share the same timeout, tenacity retries, circuit breaker and concurrency bound."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings
from app.observability.metrics import LLM_FAILOVER, LLM_TOKENS
from app.resilience import Breakers, call_upstream


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit: bool = False


class ChatLLM(Protocol):
    async def complete(self, system: str, user: str, *, feature: str, temperature: float = 0.0, fast: bool = False) -> LLMResult: ...


def fast_gateway_config(s: Settings) -> dict:
    """Single cheap-model target (query rewriting): no fallback chain needed."""
    cfg = gateway_config(s)
    cfg["strategy"] = {"mode": "single"}
    cfg["targets"] = cfg["targets"][1:]
    return cfg


def gateway_config(s: Settings) -> dict:
    return {
        "strategy": {"mode": "fallback"},
        "cache": {"mode": "simple", "max_age": 3600},
        "retry": {"attempts": 1, "on_status_codes": [429, 500, 502, 503, 504]},
        "request_timeout": int(s.llm_timeout_s * 1000 * 0.9),  # gateway gives up just before our own timeout
        "targets": [
            {"override_params": {"model": f"@{s.groq_slug}/{s.llm_primary_model}"}},
            {"override_params": {"model": f"@{s.groq_slug_fallback}/{s.llm_fallback_model}"}},
        ],
    }


class PortkeyLLM:
    def __init__(self, s: Settings, breakers: Breakers):
        from portkey_ai import AsyncPortkey

        self._s, self._breaker = s, breakers["llm"]
        self._sem = asyncio.Semaphore(s.llm_concurrency)  # backpressure toward Groq
        self._client = AsyncPortkey(
            api_key=s.portkey_api_key.get_secret_value() if s.portkey_api_key else None,
            config=gateway_config(s),
        )
        self._fast = AsyncPortkey(
            api_key=s.portkey_api_key.get_secret_value() if s.portkey_api_key else None,
            config=fast_gateway_config(s),
        )

    async def complete(self, system: str, user: str, *, feature: str, temperature: float = 0.0, fast: bool = False) -> LLMResult:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def _call():
            client = self._fast if fast else self._client
            return await client.chat.completions.create(messages=msgs, temperature=temperature)

        async with self._sem:
            resp = await call_upstream(
                self._breaker, _call, timeout_s=self._s.llm_timeout_s, attempts=self._s.max_attempts,
                retry_after_cap_s=self._s.retry_after_cap_s,
            )
        usage = getattr(resp, "usage", None)
        pt, ct = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
        LLM_TOKENS.labels(feature, "prompt").inc(pt)
        LLM_TOKENS.labels(feature, "completion").inc(ct)
        return LLMResult(resp.choices[0].message.content or "", pt, ct)


FEATURE_MAX_TOKENS = {"planner": 512, "rewrite": 256}


def failover_worthy(exc: BaseException) -> bool:
    """Should we try the other Groq key? Yes for auth/quota/server/timeout/connection problems; no for 400/404/422
    (the request itself is bad, a second key will fail the same way)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (401, 403, 408, 409, 429) or status >= 500
    return not isinstance(exc, (ValueError, TypeError))


class GroqDirectLLM:
    """Groq without the gateway. Targets are tried in order: primary key, then GROQ_FALLBACK_API_KEY (same model).
    A key problem (401/403/429/5xx/timeout) fails over inside ONE attempt; tenacity then retries the whole call with backoff."""

    def __init__(self, s: Settings, breakers: Breakers):
        from groq import AsyncGroq

        self._s, self._breaker = s, breakers["llm"]
        self._sem = asyncio.Semaphore(s.llm_concurrency)
        keys = [k for k in (s.groq_api_key, s.groq_fallback_api_key) if k]
        if not keys:
            raise RuntimeError("GROQ_API_KEY is required when USE_GATEWAY=false")
        self._clients = [AsyncGroq(api_key=k.get_secret_value(), timeout=s.llm_timeout_s, max_retries=0) for k in keys]

    def _kwargs(self, model: str, msgs: list, temperature: float, feature: str = "") -> dict:
        cap = FEATURE_MAX_TOKENS.get(feature, self._s.llm_max_tokens)  # smaller asks count less against provider TPM limits
        kw = {"model": model, "messages": msgs, "temperature": temperature, "max_completion_tokens": min(cap, self._s.llm_max_tokens)}
        if model.startswith("openai/gpt-oss") and self._s.llm_reasoning_effort:
            kw["reasoning_effort"] = self._s.llm_reasoning_effort
        return kw

    async def complete(self, system: str, user: str, *, feature: str, temperature: float = 0.0, fast: bool = False) -> LLMResult:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        models = [self._s.llm_fallback_model] if fast else [self._s.llm_primary_model]
        if not fast and self._s.llm_model_failover and self._s.llm_fallback_model not in models:
            models.append(self._s.llm_fallback_model)  # provider limits are per model: the cheaper model has its own budget
        targets = [(c, m) for m in models for c in self._clients]  # every key on the primary model first, then the cheaper model
        model = models[0]

        async def _call():
            for i, (client, m) in enumerate(targets):
                try:
                    return await client.chat.completions.create(**self._kwargs(m, msgs, temperature, feature))
                except Exception as e:
                    if i == len(targets) - 1 or not failover_worthy(e):
                        raise
                    LLM_FAILOVER.labels(type(e).__name__).inc()

        async with self._sem:
            resp = await call_upstream(self._breaker, _call, timeout_s=self._s.llm_timeout_s, attempts=self._s.max_attempts,
                                       retry_after_cap_s=self._s.retry_after_cap_s)
        usage = getattr(resp, "usage", None)
        pt, ct = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
        LLM_TOKENS.labels(feature, "prompt").inc(pt)
        LLM_TOKENS.labels(feature, "completion").inc(ct)
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        if not text:  # reasoning models can spend the whole budget thinking and return nothing
            raise ValueError(f"empty completion from {model} (finish_reason={getattr(choice, 'finish_reason', None)}); raise LLM_MAX_TOKENS")
        return LLMResult(text, pt, ct)


def make_llm(s: Settings, breakers: Breakers):
    """USE_GATEWAY=true -> Portkey (fallback chain + cache); false -> direct Groq with key failover."""
    return PortkeyLLM(s, breakers) if s.use_gateway else GroqDirectLLM(s, breakers)
