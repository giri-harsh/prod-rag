"""Redis embedding cache: key = sha256(model, dim, normalized text), 7-day TTL. Failures degrade to misses."""
from __future__ import annotations

import array
import hashlib
import re

import logfire
from prometheus_client import Counter

EMBED_CACHE = Counter("rag_embed_cache_total", "Embedding cache lookups", ["result"])


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())


def cache_key(model: str, dim: int, text: str) -> str:
    return "emb:" + hashlib.sha256(f"{model}|{dim}|{_norm(text)}".encode()).hexdigest()


def _pack(v: list[float]) -> bytes:
    return array.array("f", v).tobytes()


def _unpack(b: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(b)
    return list(a)


class CachedEmbedder:
    def __init__(self, inner, redis, ttl_s: int):
        self._inner, self._r, self._ttl = inner, redis, ttl_s
        self.model, self.dim = inner.model, inner.dim

    async def _mget(self, keys: list[str]) -> list[bytes | None]:
        try:
            return await self._r.mget(keys)
        except Exception as e:
            logfire.warn("embed cache read failed", error=str(e))
            return [None] * len(keys)

    async def _mset(self, items: dict[str, list[float]]) -> None:
        try:
            async with self._r.pipeline(transaction=False) as p:
                for k, v in items.items():
                    p.set(k, _pack(v), ex=self._ttl)
                await p.execute()
        except Exception as e:
            logfire.warn("embed cache write failed", error=str(e))

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_texts([text], _query=True))[0]

    async def embed_texts(self, texts: list[str], _query: bool = False) -> list[list[float]]:
        keys = [cache_key(self.model, self.dim, t) for t in texts]
        cached = await self._mget(keys)
        out: list[list[float] | None] = [_unpack(c) if c else None for c in cached]
        missing = [i for i, v in enumerate(out) if v is None]
        EMBED_CACHE.labels("hit").inc(len(texts) - len(missing))
        EMBED_CACHE.labels("miss").inc(len(missing))
        if missing:
            uniq = list(dict.fromkeys(texts[i] for i in missing))
            fresh = (
                [await self._inner.embed_query(uniq[0])] if _query and len(uniq) == 1 else await self._inner.embed_texts(uniq)
            )
            by_text = dict(zip(uniq, fresh, strict=True))
            for i in missing:
                out[i] = by_text[texts[i]]
            await self._mset({keys[i]: out[i] for i in missing})
        return out  # type: ignore[return-value]
