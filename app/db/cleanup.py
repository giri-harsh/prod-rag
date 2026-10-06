"""Delete conversation threads idle for more than N days.

Run as a cron (arq cron in app.worker) or manually:  python -m app.db.cleanup [days]
"""
from __future__ import annotations

import sys

import psycopg
from psycopg_pool import AsyncConnectionPool

# checkpointer tables from langgraph-checkpoint-postgres
_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

_SELECT_STALE = "SELECT thread_id FROM thread_activity WHERE last_active < now() - make_interval(days => %s)"


async def cleanup_idle_threads(pool: AsyncConnectionPool, days: int) -> int:
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(_SELECT_STALE, (days,))
            ids = [r[0] if not isinstance(r, dict) else r["thread_id"] for r in await cur.fetchall()]
            if not ids:
                return 0
            for t in _CHECKPOINT_TABLES:
                await conn.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (ids,))
            await conn.execute("DELETE FROM thread_activity WHERE thread_id = ANY(%s)", (ids,))
            return len(ids)


def cleanup_idle_threads_sync(database_url: str, days: int) -> int:
    with psycopg.connect(database_url, autocommit=False) as conn:
        ids = [r[0] for r in conn.execute(_SELECT_STALE, (days,)).fetchall()]
        for t in _CHECKPOINT_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE thread_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM thread_activity WHERE thread_id = ANY(%s)", (ids,))
        conn.commit()
        return len(ids)


if __name__ == "__main__":
    from app.config import load_settings

    s = load_settings()
    d = int(sys.argv[1]) if len(sys.argv) > 1 else s.memory_ttl_days
    print(f"deleted {cleanup_idle_threads_sync(s.database_url, d)} idle threads (>{d}d)")
