"""Redis-backed planner decision cache (tenant-scoped, TTL). Cache failures are treated as misses."""
from __future__ import annotations

import hashlib
import re

import logfire


def normalize(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


class PlannerCache:
    def __init__(self, redis, ttl_s: int):
        self._r, self._ttl = redis, ttl_s

    @staticmethod
    def key(tenant_id: str, query: str, history: str) -> str:
        h = hashlib.sha256(f"{normalize(query)}\x00{history}".encode()).hexdigest()
        return f"planner:{tenant_id}:{h}"

    async def get(self, key: str) -> str | None:
        try:
            v = await self._r.get(key)
            return v.decode() if isinstance(v, bytes) else v
        except Exception as e:
            logfire.warn("planner cache get failed", error=str(e))
            return None

    async def set(self, key: str, value: str) -> None:
        try:
            await self._r.set(key, value, ex=self._ttl)
        except Exception as e:
            logfire.warn("planner cache set failed", error=str(e))
