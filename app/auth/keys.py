"""API key storage (sha256 hashed) and lookup."""
from __future__ import annotations

import hashlib
import re
import secrets
import time
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
) -> str:
    """Create a key and return the RAW value. It is shown once and never stored."""
    if not (ID_RE.match(tenant_id) and ID_RE.match(user_id)):
        raise ValueError("tenant_id/user_id must match [A-Za-z0-9_.-]{1,64}")
    raw = PREFIX + secrets.token_urlsafe(32)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, tenant_id, user_id, is_admin, rate_per_min, rate_per_day, label) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (hash_key(raw), tenant_id, user_id, is_admin, rate_per_min, rate_per_day, label),
        )
    return raw


def revoke_key(database_url: str, *, raw: str | None = None, key_id: str | None = None) -> int:
    if not (raw or key_id):
        raise ValueError("provide a raw key or a key id")
    with psycopg.connect(database_url) as conn:
        if raw:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE key_hash=%s AND revoked_at IS NULL", (hash_key(raw),)
            )
        else:
            cur = conn.execute("UPDATE api_keys SET revoked_at = now() WHERE id=%s AND revoked_at IS NULL", (key_id,))
        return cur.rowcount


def list_keys(database_url: str) -> list[tuple]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT id, tenant_id, user_id, is_admin, rate_per_min, rate_per_day, label, revoked_at FROM api_keys ORDER BY created_at"
        ).fetchall()


# ---- async lookup (request path) --------------------------------------------------------------
class KeyStore:
    """Looks up keys by hash in Postgres with a tiny TTL cache (bounds revocation latency)."""

    def __init__(self, pool, cache_ttl_s: float = 10.0):
        self._pool, self._ttl = pool, cache_ttl_s
        self._cache: dict[str, tuple[float, Principal | None]] = {}

    async def lookup(self, raw: str) -> Principal | None:
        h = hash_key(raw)
        hit = self._cache.get(h)
        now = time.monotonic()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, tenant_id, user_id, is_admin, rate_per_min, rate_per_day FROM api_keys "
                "WHERE key_hash=%s AND revoked_at IS NULL",
                (h,),
            )
            row = await cur.fetchone()
        p = None
        if row:
            row = tuple(row.values()) if isinstance(row, dict) else row
            p = Principal(str(row[0]), row[1], row[2], row[3], row[4], row[5])
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[h] = (now, p)
        return p
