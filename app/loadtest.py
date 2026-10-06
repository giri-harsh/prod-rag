"""APP_ENV=loadtest: fake LLM / embedder / guard model with realistic simulated latency.

Postgres, Redis and Qdrant stay REAL; only the paid upstreams are replaced. Latencies come from settings
(loadtest_llm_ms +- jitter, loadtest_embed_ms +- jitter, loadtest_guard_ms).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import Settings
from app.services.llm import LLMResult

_GREETING = re.compile(r"^(hi|hello|hey|thanks|thank you)\b", re.I)


async def _sleep(ms: float, jitter: float) -> None:
    await asyncio.sleep(max(1.0, random.gauss(ms, jitter)) / 1000.0)


class LoadEmbedder:
    def __init__(self, s: Settings):
        self.model, self.dim, self._s = "loadtest-embed", s.embedding_dim, s

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0  # noqa: S324
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed_query(self, text: str) -> list[float]:
        await _sleep(self._s.loadtest_embed_ms, self._s.loadtest_embed_jitter_ms)
        return self._vec(text)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        await _sleep(self._s.loadtest_embed_ms, self._s.loadtest_embed_jitter_ms)
        return [self._vec(t) for t in texts]


class LoadLLM:
    def __init__(self, s: Settings):
        self._s = s

    async def complete(self, system, user, *, feature, temperature=0.0, fast=False):
        await _sleep(self._s.loadtest_llm_ms * (0.4 if fast else 1.0), self._s.loadtest_llm_jitter_ms)
        if feature == "planner":
            msg = re.search(r"<<<(.*)>>>", user, re.S).group(1).strip()
            has_hist = "HISTORY:\n(none)" not in user
            if _GREETING.match(msg) or "my name" in msg.lower():
                return LLMResult(json.dumps({"intent": "conversational", "query": "", "standalone": False}), 400, 20)
            follow = has_hist and msg.lower().startswith(("and ", "what about", "how about", "why"))
            return LLMResult(json.dumps({"intent": "search", "query": msg, "standalone": not follow}), 450, 30)
        if feature == "rewrite":
            return LLMResult("kubernetes networking scheduling", 200, 10)
        return LLMResult("Simulated grounded answer based on the retrieved context.", 1800, 120)


class LoadGuardLLM(BaseChatModel):
    """Stands in for the Groq 8B guard model: answers Yes (block) for obvious jailbreak text, else No."""

    ms: float = 250

    def __init__(self, s: Settings, **kw):
        super().__init__(ms=s.loadtest_guard_ms, **kw)

    @property
    def _llm_type(self) -> str:
        return "loadtest-guard"

    def _decide(self, messages) -> ChatResult:
        text = str(messages[-1].content).lower()
        bad = any(t in text for t in ("ignore all previous", "ignore previous", "jailbreak", "system prompt", "you are now dan"))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Yes" if bad else "No"))])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._decide(messages)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(max(1.0, random.gauss(self.ms, self.ms * 0.3)) / 1000.0)
        return self._decide(messages)
