"""Delete conversation threads idle for more than N days.

Run as a cron (arq cron in app.worker) or manually:  python -m app.db.cleanup [days]
"""
from __future__ import annotations

import sys

import psycopg
from psycopg_pool import AsyncConnectionPool

# checkpointer tables from langgraph-checkpoint-postgres (static statements: no string-built SQL)
_DELETE_STATEMENTS = (
    "DELETE FROM checkpoint_writes WHERE thread_id = ANY(%s)",
    "DELETE FROM checkpoint_blobs WHERE thread_id = ANY(%s)",
    "DELETE FROM checkpoints WHERE thread_id = ANY(%s)",
)

_SELECT_STALE = "SELECT thread_id FROM thread_activity WHERE last_active < now() - make_interval(days => %s)"


async def cleanup_idle_threads(pool: AsyncConnectionPool, days: int) -> int:
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(_SELECT_STALE, (days,))
            ids = [r[0] if not isinstance(r, dict) else r["thread_id"] for r in await cur.fetchall()]
            if not ids:
                return 0
            for stmt in _DELETE_STATEMENTS:
                await conn.execute(stmt, (ids,))
            await conn.execute("DELETE FROM thread_activity WHERE thread_id = ANY(%s)", (ids,))
            return len(ids)


def cleanup_idle_threads_sync(database_url: str, days: int) -> int:
    with psycopg.connect(database_url, autocommit=False) as conn:
        ids = [r[0] for r in conn.execute(_SELECT_STALE, (days,)).fetchall()]
        for stmt in _DELETE_STATEMENTS:
            conn.execute(stmt, (ids,))
        conn.execute("DELETE FROM thread_activity WHERE thread_id = ANY(%s)", (ids,))
        conn.commit()
        return len(ids)


if __name__ == "__main__":
    from app.config import load_settings

    s = load_settings()
    d = int(sys.argv[1]) if len(sys.argv) > 1 else s.memory_ttl_days
    print(f"deleted {cleanup_idle_threads_sync(s.database_url, d)} idle threads (>{d}d)")
