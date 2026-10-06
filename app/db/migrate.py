"""Apply SQL migrations + create LangGraph checkpoint tables.  Usage: python -m app.db.migrate"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).parent / "migrations"
LOCK_ID = 727_001  # advisory lock so concurrent deploys don't race


def migrate(database_url: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name in done:
                    continue
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied.append(f.name)
            # LangGraph checkpointer tables (idempotent)
            from langgraph.checkpoint.postgres import PostgresSaver

            PostgresSaver(conn).setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied


if __name__ == "__main__":
    from app.config import load_settings

    s = load_settings()
    print("applied:", migrate(s.database_url) or "nothing (up to date)")
    sys.exit(0)
