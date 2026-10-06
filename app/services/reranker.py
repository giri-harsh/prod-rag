"""FlashRank reranker: loaded once at startup, scored off the event loop."""
from __future__ import annotations

import asyncio
from typing import Protocol

import logfire


class Reranker(Protocol):
    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[str]: ...


class FlashRankReranker:
    def __init__(self, cache_dir: str = "/opt/flashrank"):
        from flashrank import Ranker

        self._ranker = Ranker(cache_dir=cache_dir)  # model is baked into the image at build time

    def _score(self, query: str, docs: list[str], top_n: int) -> list[str]:
        from flashrank import RerankRequest

        res = self._ranker.rerank(RerankRequest(query=query, passages=[{"id": i, "text": d} for i, d in enumerate(docs)]))
        return [r["text"] for r in res[:top_n]]

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[str]:
        if not docs:
            return []
        try:
            return await asyncio.to_thread(self._score, query, docs, top_n)
        except Exception as e:  # degrade to vector order rather than failing the request
            logfire.error("rerank failed; using vector order", error=str(e))
            return docs[:top_n]


class NoopReranker:
    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[str]:
        return docs[:top_n]
