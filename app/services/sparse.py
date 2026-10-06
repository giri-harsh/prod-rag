"""Sparse (lexical) encoders for hybrid search. Qdrant applies the IDF modifier server-side for both."""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from typing import Protocol

import logfire
from qdrant_client import models

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has in is it of on or that the to was were will with this these those".split())


class SparseEncoder(Protocol):
    name: str

    async def encode_docs(self, texts: list[str]) -> list[models.SparseVector]: ...
    async def encode_query(self, text: str) -> models.SparseVector: ...


class SimpleSparseEncoder:
    """Fallback: lowercase tokenizer -> hashed term ids, raw term frequency (server-side IDF supplies the weighting)."""

    name = "simple"

    @staticmethod
    def _vec(text: str) -> models.SparseVector:
        tf = Counter(t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1)
        idx = {}
        for tok, n in tf.items():
            i = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
            idx[i] = idx.get(i, 0.0) + float(n)
        return models.SparseVector(indices=list(idx), values=list(idx.values()))

    async def encode_docs(self, texts):
        return [self._vec(t) for t in texts]

    async def encode_query(self, text):
        return self._vec(text)


class Bm25SparseEncoder:
    """fastembed `Qdrant/bm25` (stemming + stopwords; no neural model). CPU work runs off the event loop."""

    name = "bm25"

    def __init__(self):
        from fastembed import SparseTextEmbedding

        self._m = SparseTextEmbedding("Qdrant/bm25")

    @staticmethod
    def _conv(e) -> models.SparseVector:
        return models.SparseVector(indices=[int(i) for i in e.indices], values=[float(v) for v in e.values])

    async def encode_docs(self, texts):
        embs = await asyncio.to_thread(lambda: list(self._m.embed(texts)))
        return [self._conv(e) for e in embs]

    async def encode_query(self, text):
        embs = await asyncio.to_thread(lambda: list(self._m.query_embed(text)))
        return self._conv(embs[0])


def make_sparse_encoder(backend: str) -> SparseEncoder:
    if backend == "bm25":
        try:
            return Bm25SparseEncoder()
        except Exception as e:  # fastembed missing / stopword files unavailable
            logfire.warn("fastembed BM25 unavailable; using built-in simple sparse encoder", error=str(e))
    return SimpleSparseEncoder()
