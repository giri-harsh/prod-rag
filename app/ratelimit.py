"""Per-API-key rate limits (per-minute + per-day fixed windows) in Redis, fully async.

Why not slowapi: it drives the *sync* `limits` storage (blocks the event loop) and its async redis
backend needs `coredis`. Two atomic INCR+EXPIRE pipelines on redis.asyncio give the same semantics
with fewer moving parts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import logfire

from app.auth.keys import Principal
from app.observability.metrics import RATE_LIMITED


@dataclass
class Limited(Exception):
    retry_after: int
    window: str


class RateLimiter:
    def __init__(self, redis):
        self._r = redis

    async def _hit(self, key: str, ttl: int) -> int:
        async with self._r.pipeline(transaction=True) as p:
            p.incr(key)
            p.expire(key, ttl, nx=True)
            count, _ = await p.execute()
        return int(count)

    async def check(self, p: Principal) -> None:
        """Raise Limited when over quota. If Redis is down we fail OPEN (logged + metric) to keep serving."""
        now = int(time.time())
        try:
            for window, size, limit in (("minute", 60, p.rate_per_min), ("day", 86400, p.rate_per_day)):
                bucket = now // size
                if await self._hit(f"rl:{p.key_id}:{window}:{bucket}", size * 2) > limit:
                    RATE_LIMITED.labels(window).inc()
                    raise Limited(max(1, (bucket + 1) * size - now), window)
        except Limited:
            raise
        except Exception as e:
            logfire.error("rate limiter backend failure; failing open", error=str(e))
