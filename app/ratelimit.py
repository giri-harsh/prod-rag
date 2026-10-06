"""Per-API-key rate limits (per-minute + per-day fixed windows) in Redis, fully async.

Why not slowapi: it drives the *sync* `limits` storage (blocks the event loop) and its async redis
backend needs `coredis`. Two atomic INCR+EXPIRE pipelines on redis.asyncio give the same semantics.

If Redis is unavailable we do NOT fail open: each worker falls back to an in-process token bucket whose
limit is (per-key limit / worker count), so the fleet-wide ceiling stays roughly the configured one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import logfire
from prometheus_client import Counter

from app.auth.keys import Principal
from app.observability.metrics import RATE_LIMITED

LIMITER_FALLBACK = Counter("rag_limiter_fallback_total", "Requests rate-limited by the in-process fallback (Redis down)")


@dataclass
class Limited(Exception):
    retry_after: int
    window: str


class TokenBucket:
    def __init__(self, per_minute: float, clock=time.monotonic):
        self.capacity = max(1.0, per_minute)
        self.rate = self.capacity / 60.0
        self.tokens, self._clock = self.capacity, clock
        self._t = clock()

    def take(self) -> float:
        """0.0 if a token was taken, otherwise seconds until one is available."""
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._t) * self.rate)
        self._t = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate


class RateLimiter:
    def __init__(self, redis, workers: int = 1, clock=time.monotonic):
        self._r, self._workers, self._clock = redis, max(1, workers), clock
        self._buckets: dict[str, TokenBucket] = {}

    async def _hit(self, key: str, ttl: int) -> int:
        async with self._r.pipeline(transaction=True) as p:
            p.incr(key)
            p.expire(key, ttl, nx=True)
            count, _ = await p.execute()
        return int(count)

    def _fallback(self, p: Principal) -> None:
        b = self._buckets.get(p.key_id)
        if b is None:
            if len(self._buckets) > 50_000:
                self._buckets.clear()
            b = self._buckets[p.key_id] = TokenBucket(p.rate_per_min / self._workers, self._clock)
        wait = b.take()
        if wait > 0:
            LIMITER_FALLBACK.inc()
            RATE_LIMITED.labels("fallback").inc()
            raise Limited(max(1, int(wait) + 1), "fallback")

    async def check(self, p: Principal) -> None:
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
            logfire.error("rate limiter redis failure; using in-process fallback", error=str(e))
            self._fallback(p)
