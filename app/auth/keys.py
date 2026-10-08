"""API key storage (sha256 hashed) and lookup."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

import psycopg

PREFIX = "rag_"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # no ":" so thread ids cannot collide across tenants


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    key_id: str
    tenant_id: str
    user_id: str
    is_admin: bool
    rate_per_min: int
    rate_per_day: int
    scopes: tuple[str, ...] = ()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ---- sync admin operations (CLI / seed) -------------------------------------------------------
def create_key(
    database_url: str,
    tenant_id: str,
    user_id: str,
    *,
    is_admin: bool = False,
    rate_per_min: int = 60,
    rate_per_day: int = 10_000,
    label: str | None = None,
    scopes: tuple[str, ...] = (),
    redis_url: str | None = None,
) -> str:
    """Create a key and return the RAW value. It is shown once and never stored."""
    if not (ID_RE.match(tenant_id) and ID_RE.match(user_id)):
        raise ValueError("tenant_id/user_id must match [A-Za-z0-9_.-]{1,64}")
    raw = PREFIX + secrets.token_urlsafe(32)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, tenant_id, user_id, is_admin, rate_per_min, rate_per_day, label, scopes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (hash_key(raw), tenant_id, user_id, is_admin, rate_per_min, rate_per_day, label, list(scopes)),
        )
    publish_evict(redis_url, [hash_key(raw)])  # clears any negative-cache entry for this (previously unknown) hash
    return raw


def revoke_key(database_url: str, *, raw: str | None = None, key_id: str | None = None, redis_url: str | None = None) -> int:
    """Revoke a key. With `redis_url`, every API worker evicts it from its cache immediately (pub/sub); otherwise within the cache TTL."""
    if not (raw or key_id):
        raise ValueError("provide a raw key or a key id")
    with psycopg.connect(database_url) as conn:
        if raw:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE key_hash=%s AND revoked_at IS NULL RETURNING key_hash", (hash_key(raw),)
            )
        else:
            cur = conn.execute("UPDATE api_keys SET revoked_at = now() WHERE id=%s AND revoked_at IS NULL RETURNING key_hash", (key_id,))
        hashes = [r[0] for r in cur.fetchall()]
    publish_evict(redis_url, hashes)
    return len(hashes)


def list_keys(database_url: str) -> list[tuple]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT id, tenant_id, user_id, is_admin, rate_per_min, rate_per_day, label, revoked_at FROM api_keys ORDER BY created_at"
        ).fetchall()


# ---- async lookup (request path) --------------------------------------------------------------
log = logging.getLogger(__name__)
EVICT_CHANNEL = "auth:evict"
NEG_PREFIX = "auth:neg:"


def publish_evict(redis_url: str | None, key_hashes: list[str]) -> None:
    """Sync helper for the CLI: drop negative-cache entries and tell every API worker to evict these key hashes now."""
    if not redis_url or not key_hashes:
        return
    try:
        import redis as sync_redis

        r = sync_redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        for h in key_hashes:
            r.delete(NEG_PREFIX + h)
            r.publish(EVICT_CHANNEL, h)
        r.close()
    except Exception as e:  # the TTL is the fallback
        logging.getLogger(__name__).warning("could not publish key eviction: %s", type(e).__name__)


class KeyStore:
    """Key lookup with three layers in front of Postgres:
    1. in-process LRU of VALID keys (TTL `ttl_s`, size `maxsize`), evicted instantly on revoke via Redis pub/sub;
    2. Redis negative cache of unknown key hashes (`neg_ttl_s`);
    3. Postgres. `pg_lookups` counts how often we get here."""

    def __init__(self, pool, cache_ttl_s: float = 30.0, redis=None, neg_ttl_s: int = 60, maxsize: int = 10_000):
        self._pool, self._ttl, self._redis, self._neg_ttl, self._max = pool, cache_ttl_s, redis, neg_ttl_s, maxsize
        self._lru: OrderedDict[str, tuple[float, Principal]] = OrderedDict()
        self._local_neg: dict[str, float] = {}
        self._listener: asyncio.Task | None = None
        self.pg_lookups = 0

    # -- layer 1 ----------------------------------------------------------------------------
    def peek(self, raw: str) -> Principal | None:
        """LRU-only lookup (no I/O)."""
        h = hash_key(raw)
        hit = self._lru.get(h)
        if hit and time.monotonic() - hit[0] < self._ttl:
            self._lru.move_to_end(h)
            return hit[1]
        if hit:
            self._lru.pop(h, None)
        return None

    def evict(self, h: str) -> None:
        self._lru.pop(h, None)
        self._local_neg.pop(h, None)

    def _remember(self, h: str, p: Principal) -> None:
        if self._ttl <= 0:
            return
        self._lru[h] = (time.monotonic(), p)
        self._lru.move_to_end(h)
        while len(self._lru) > self._max:
            self._lru.popitem(last=False)

    # -- full lookup ------------------------------------------------------------------------
    async def lookup(self, raw: str) -> Principal | None:
        if (p := self.peek(raw)) is not None:
            return p
        h = hash_key(raw)
        now = time.monotonic()
        if self._neg_ttl > 0:
            if self._local_neg.get(h, 0) > now:
                return None
            if self._redis is not None:
                try:
                    if await self._redis.exists(NEG_PREFIX + h):
                        self._local_neg[h] = now + min(self._neg_ttl, 5)
                        return None
                except Exception as e:  # Redis is an optimisation here
                    log.debug("negative-cache read failed: %s", type(e).__name__)
        self.pg_lookups += 1
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, tenant_id, user_id, is_admin, rate_per_min, rate_per_day, scopes FROM api_keys "
                "WHERE key_hash=%s AND revoked_at IS NULL",
                (h,),
            )
            row = await cur.fetchone()
        if not row:
            if self._neg_ttl > 0:
                if len(self._local_neg) > self._max:
                    self._local_neg.clear()
                self._local_neg[h] = now + min(self._neg_ttl, 5)
                if self._redis is not None:
                    try:
                        await self._redis.set(NEG_PREFIX + h, "1", ex=self._neg_ttl)
                    except Exception as e:
                        log.debug("negative-cache write failed: %s", type(e).__name__)
            return None
        row = tuple(row.values()) if isinstance(row, dict) else row
        p = Principal(str(row[0]), row[1], row[2], row[3], row[4], row[5], tuple(row[6] or ()))
        self._remember(h, p)
        return p

    # -- revocation fan-out -----------------------------------------------------------------
    async def start_listener(self) -> None:
        if self._redis is None or self._listener is not None:
            return
        ps = self._redis.pubsub()
        await ps.subscribe(EVICT_CHANNEL)
        self._listener = asyncio.create_task(self._listen(ps))

    async def _listen(self, ps) -> None:
        try:
            while True:
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    d = msg["data"]
                    self.evict(d.decode() if isinstance(d, bytes) else d)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.getLogger(__name__).warning("key eviction listener stopped (%s); TTL is the fallback", type(e).__name__)
        finally:
            try:
                await ps.aclose()
            except Exception as e:
                log.debug("pubsub close failed: %s", type(e).__name__)

    async def stop_listener(self) -> None:
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except BaseException:  # noqa: BLE001
                pass
            self._listener = None


def set_tenant_domain(database_url: str, tenant_id: str, domain: str | None) -> None:
    if not ID_RE.match(tenant_id):
        raise ValueError("tenant_id must match [A-Za-z0-9_.-]{1,64}")
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO tenants (tenant_id, domain_description) VALUES (%s,%s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET domain_description=EXCLUDED.domain_description, updated_at=now()",
            (tenant_id, domain),
        )


class TenantConfig:
    """Per-tenant settings (currently the assistant domain) with a short in-process TTL cache."""

    def __init__(self, pool, default_domain: str, ttl_s: float = 60.0):
        self._pool, self._default, self._ttl = pool, default_domain, ttl_s
        self._cache: dict[str, tuple[float, str]] = {}

    async def domain(self, tenant_id: str) -> str:
        hit = self._cache.get(tenant_id)
        now = time.monotonic()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        value = self._default
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute("SELECT domain_description FROM tenants WHERE tenant_id=%s", (tenant_id,))
                row = await cur.fetchone()
            if row:
                v = row["domain_description"] if isinstance(row, dict) else row[0]
                value = v or self._default
        except Exception as e:  # config lookup must never take the request down; fall back to the global default
            logging.getLogger(__name__).warning("tenant config lookup failed: %s", type(e).__name__)
        self._cache[tenant_id] = (now, value)
        return value
