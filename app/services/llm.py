"""Async chat LLM via Portkey (Groq models) with fallback, bounded concurrency, retries and breaker."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings
from app.observability.metrics import LLM_TOKENS
from app.resilience import Breakers, call_upstream


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit: bool = False


class ChatLLM(Protocol):
    async def complete(self, system: str, user: str, *, feature: str, temperature: float = 0.0) -> LLMResult: ...


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

    async def complete(self, system: str, user: str, *, feature: str, temperature: float = 0.0) -> LLMResult:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def _call():
            return await self._client.chat.completions.create(messages=msgs, temperature=temperature)

        async with self._sem:
            resp = await call_upstream(
                self._breaker, _call, timeout_s=self._s.llm_timeout_s, attempts=self._s.max_attempts
            )
        usage = getattr(resp, "usage", None)
        pt, ct = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
        LLM_TOKENS.labels(feature, "prompt").inc(pt)
        LLM_TOKENS.labels(feature, "completion").inc(ct)
        return LLMResult(resp.choices[0].message.content or "", pt, ct)
