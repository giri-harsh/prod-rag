"""Embeddings: Gemini GA model, explicit dimensionality, hard failure (no silent local fallback)."""
from __future__ import annotations

import math
from typing import Protocol

from app.config import Settings
from app.resilience import Breakers, call_upstream

API_BATCH = 100  # Gemini batchEmbedContents limit


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_query(self, text: str) -> list[float]: ...
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class GeminiEmbedder:
    def __init__(self, s: Settings, breakers: Breakers):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        self.model, self.dim = s.embedding_model, s.embedding_dim
        self._s, self._breaker = s, breakers["embeddings"]
        self._client = GoogleGenerativeAIEmbeddings(
            model=f"models/{s.embedding_model}",
            google_api_key=s.gemini_api_key.get_secret_value() if s.gemini_api_key else None,
            output_dimensionality=s.embedding_dim,
        )

    async def embed_query(self, text: str) -> list[float]:
        v = await call_upstream(
            self._breaker, lambda: self._client.aembed_query(text),
            timeout_s=self._s.embed_timeout_s, attempts=self._s.max_attempts,
            retry_after_cap_s=self._s.retry_after_cap_s,
        )
        return self._check(_normalize(v))

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), API_BATCH):
            batch = texts[i : i + API_BATCH]
            vs = await call_upstream(
                self._breaker, lambda b=batch: self._client.aembed_documents(b),
                timeout_s=self._s.embed_timeout_s * 3, attempts=self._s.max_attempts,
                retry_after_cap_s=self._s.retry_after_cap_s,
            )
            out.extend(self._check(_normalize(v)) for v in vs)
        return out

    def _check(self, v: list[float]) -> list[float]:
        if len(v) != self.dim:
            raise ValueError(f"embedding dim {len(v)} != configured {self.dim}")
        return v
