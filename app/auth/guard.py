"""Client-IP resolution (X-Forwarded-For only from trusted proxies) and the per-IP failed-auth limiter."""
from __future__ import annotations

import ipaddress
import time

import logfire

from app.observability.metrics import RATE_LIMITED


def parse_trusted(spec: str) -> list:
    nets = []
    for part in (x.strip() for x in spec.split(",")):
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _in(ip: str, nets: list) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)


def client_ip(peer: str | None, xff: str | None, trusted: list) -> str:
    """The real client address. X-Forwarded-For is honoured ONLY when the direct peer is a trusted proxy; then we walk the
    chain from the right, skipping trusted hops, and take the first untrusted address (the left-most entries are attacker-controlled)."""
    peer = peer or "unknown"
    if not xff or not trusted or not _in(peer, trusted):
        return peer
    for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
        if not _in(hop, trusted):
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                return peer
    return peer


class AuthFailLimiter:
    """Counts FAILED authentications per client IP per minute in Redis; over the limit => 429 instead of 401."""

    def __init__(self, redis, limit: int):
        self._r, self.limit = redis, limit

    @staticmethod
    def _key(ip: str) -> str:
        return f"authfail:{ip}:{int(time.time()) // 60}"

    async def exceeded(self, ip: str) -> bool:
        try:
            n = await self._r.get(self._key(ip))
            return int(n or 0) >= self.limit
        except Exception:
            return False  # Redis down: do not lock everyone out

    async def record_failure(self, ip: str) -> int:
        try:
            k = self._key(ip)
            async with self._r.pipeline(transaction=True) as p:
                p.incr(k)
                p.expire(k, 120, nx=True)
                n, _ = await p.execute()
            if n > self.limit:
                RATE_LIMITED.labels("auth_fail").inc()
            return int(n)
        except Exception as e:
            logfire.warn("auth-fail limiter unavailable", error=str(e))
            return 0

    @staticmethod
    def retry_after() -> int:
        return 60 - int(time.time()) % 60
