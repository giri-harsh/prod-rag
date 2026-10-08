"""Coordination for blue/green index rebuilds.

* One rebuild per alias at a time: Redis `SET NX EX` lock with a heartbeat that renews it while the holder is alive.
  If Redis is unreachable we fall back to a Postgres session advisory lock (mutual exclusion only).
* While a rebuild runs, its state ({live, candidate}) lives in Redis under the same TTL. Every write that targets the live
  collection is mirrored to the candidate (see VectorStore.write_points / delete_where), so nothing written during the
  rebuild is lost at the swap. A reconciliation pass in the pipeline repairs whatever the mirror missed.
* If the holder dies: the heartbeat stops, the lock and state expire after the TTL, mirroring stops, the live alias was never
  touched, and the next rebuild garbage-collects the orphaned candidate.
"""
from __future__ import annotations

import asyncio
import json
import uuid
import zlib
from dataclasses import dataclass, field

import logfire

from app.errors import AppError


class RebuildInProgress(AppError):
    status_code = 409
    public_message = "A rebuild of this index is already running"
    retry_after = 30


@dataclass
class RebuildHandle:
    token: str
    heartbeat: asyncio.Task | None = None
    pg_conn: object | None = None
    via: str = "redis"
    candidate: str | None = field(default=None)


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


class RebuildCoordinator:
    def __init__(self, redis, alias: str, database_url: str | None = None, ttl_s: int = 60):
        self._r, self.alias, self._dsn, self.ttl = redis, alias, database_url, max(2, int(ttl_s))
        self.lock_key = f"rebuild:{alias}:lock"
        self.state_key = f"rebuild:{alias}:state"

    # ---- queries -------------------------------------------------------------------------------
    async def is_locked(self) -> bool:
        try:
            return bool(await self._r.exists(self.lock_key))
        except Exception:
            return False

    async def mirror_target(self, live: str) -> str | None:
        """Candidate collection to mirror writes to, if a rebuild of `live` is in progress."""
        try:
            raw = _s(await self._r.get(self.state_key))
        except Exception:
            return None
        if not raw:
            return None
        st = json.loads(raw)
        return st["candidate"] if st.get("live") == live else None

    # ---- lifecycle -----------------------------------------------------------------------------
    async def acquire(self) -> RebuildHandle:
        token = uuid.uuid4().hex
        try:
            ok = await self._r.set(self.lock_key, token, nx=True, ex=self.ttl)
            if not ok:
                raise RebuildInProgress(self.alias)
            h = RebuildHandle(token)
            h.heartbeat = asyncio.create_task(self._beat(h))
            return h
        except RebuildInProgress:
            raise
        except Exception as e:  # Redis down: fall back to a Postgres advisory lock (no mirroring without Redis)
            logfire.warn("redis unavailable for rebuild lock; trying postgres advisory lock", error=str(e))
            return await self._acquire_pg(token)

    async def _acquire_pg(self, token: str) -> RebuildHandle:
        if not self._dsn:
            raise RebuildInProgress("no lock backend available")
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (zlib.crc32(self.alias.encode()),))
        if not (await cur.fetchone())[0]:
            await conn.close()
            raise RebuildInProgress(self.alias)
        return RebuildHandle(token, pg_conn=conn, via="postgres")

    async def set_state(self, h: RebuildHandle, live: str, candidate: str) -> None:
        h.candidate = candidate
        if h.via != "redis":
            return
        try:
            await self._r.set(self.state_key, json.dumps({"live": live, "candidate": candidate, "token": h.token}), ex=self.ttl)
        except Exception as e:
            logfire.warn("could not publish rebuild state; mirroring disabled (reconciliation will repair)", error=str(e))

    async def _beat(self, h: RebuildHandle) -> None:
        while True:
            await asyncio.sleep(self.ttl / 3)
            try:
                if _s(await self._r.get(self.lock_key)) != h.token:
                    return  # lost the lock (expired): stop renewing
                await self._r.expire(self.lock_key, self.ttl)
                if await self._r.exists(self.state_key):
                    await self._r.expire(self.state_key, self.ttl)
            except Exception as e:
                logfire.warn("rebuild heartbeat failed", error=str(e))

    async def release(self, h: RebuildHandle | None) -> None:
        if h is None:
            return
        if h.heartbeat:
            h.heartbeat.cancel()
            try:
                await h.heartbeat
            except BaseException:  # noqa: BLE001 - cancellation of our own task
                pass
        if h.pg_conn is not None:
            try:
                await h.pg_conn.close()  # closing the session releases the advisory lock
            except Exception:
                pass
            return
        try:
            if _s(await self._r.get(self.lock_key)) == h.token:
                await self._r.delete(self.lock_key, self.state_key)
        except Exception as e:
            logfire.warn("rebuild lock release failed (expires on its own)", error=str(e))
