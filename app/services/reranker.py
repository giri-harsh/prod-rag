"""FlashRank reranker: loaded once at startup, scored off the event loop. Returns (text, score) best-first."""
from __future__ import annotations

import asyncio
from typing import Protocol

import logfire

Scored = list[tuple[str, float]]


class Reranker(Protocol):
    async def rerank(self, query: str, docs: list[str], top_n: int) -> Scored: ...


class FlashRankReranker:
    def __init__(self, cache_dir: str = "/opt/flashrank"):
        from flashrank import Ranker

        self._ranker = Ranker(cache_dir=cache_dir)  # model is baked into the image at build time

    def _score(self, query: str, docs: list[str], top_n: int) -> Scored:
        from flashrank import RerankRequest

        res = self._ranker.rerank(RerankRequest(query=query, passages=[{"id": i, "text": d} for i, d in enumerate(docs)]))
        return [(r["text"], float(r["score"])) for r in res[:top_n]]

    async def rerank(self, query: str, docs: list[str], top_n: int) -> Scored:
        if not docs:
            return []
        try:
            return await asyncio.to_thread(self._score, query, docs, top_n)
        except Exception as e:  # degrade to fused order (score 1.0 so grading does not loop) rather than failing
            logfire.error("rerank failed; using fused order", error=str(e))
            return [(d, 1.0) for d in docs[:top_n]]


class NoopReranker:
    async def rerank(self, query: str, docs: list[str], top_n: int) -> Scored:
        return [(d, 1.0) for d in docs[:top_n]]
