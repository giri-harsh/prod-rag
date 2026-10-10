"""Row-level fingerprint of the application database, for proving a restore actually brought the data back.

    python scripts/db_fingerprint.py                    # fingerprint $DIRECT_DATABASE_URL or $DATABASE_URL
    python scripts/db_fingerprint.py --db rag_restored  # same server, another database
    python scripts/db_fingerprint.py --json out.json
    python scripts/db_fingerprint.py --compare before.json   # exit 1 if anything differs

`scripts/backup_restore.sh restore` reports TOC entries and a table count, which proves the schema arrived.
It does not prove the rows did, and a LangGraph thread is only usable if its checkpoints came back intact -
so this hashes the durable state that matters: per-table row counts plus a digest over the primary keys of
the checkpoint tables and over tenant/thread identity.

Output is counts and digests only. Row content is hashed, never printed, and no connection string is shown,
so a fingerprint is safe to paste into a runbook or an incident ticket.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

PK_COLUMNS = """
SELECT a.attname
  FROM pg_index i
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
 WHERE i.indrelid = %s::regclass AND i.indisprimary
 ORDER BY array_position(i.indkey, a.attnum)
"""


def fingerprint(conn) -> dict:
    """Per-table row count plus a sha256 over the primary keys, for every table in the public schema.

    The keys are discovered from the live schema rather than hardcoded, so a new table is covered the day it
    is added and a renamed column cannot leave a silently unchecked table behind. Rows are hashed in key
    order, which makes the digest independent of physical row order - a dump/restore reorders rows freely.
    A table with no primary key is counted but not hashed, and is listed so the gap is visible.
    """
    tables = sorted(
        r[0]
        for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    )
    out: dict[str, dict] = {}
    unkeyed = []
    for table in tables:
        n = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        keys = [r[0] for r in conn.execute(PK_COLUMNS, (table,))]
        if not keys:
            unkeyed.append(table)
            out[table] = {"rows": n, "keys": None, "keys_sha256": None}
            continue
        cols = ", ".join(f'"{c}"::text' for c in keys)
        order = ", ".join(f'"{c}"' for c in keys)
        rows = conn.execute(f'SELECT {cols} FROM "{table}" ORDER BY {order}').fetchall()
        h = hashlib.sha256()
        for row in rows:
            h.update(("\x1f".join("" if v is None else v for v in row) + "\x1e").encode())
        # Whole-row digest as well: matching keys only prove the rows are *present*. A checkpoint whose blob
        # came back truncated or re-encoded has the same key and is still unusable, so hash every column.
        # t::text renders bytea as hex, so this covers the checkpoint payloads themselves.
        content = conn.execute(
            f'SELECT md5(coalesce(string_agg(t::text, %s ORDER BY {order}), %s)) FROM "{table}" t'
        , ("\x1e", "")).fetchone()[0]
        out[table] = {"rows": n, "keys": keys, "keys_sha256": h.hexdigest()[:16],
                      "content_md5": content[:16]}
    return {"tables": out, "tables_without_a_primary_key": unkeyed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="override the database name in the URL")
    ap.add_argument("--json", default="", help="also write the fingerprint here")
    ap.add_argument("--compare", default="", help="compare against a fingerprint file; exit 1 on any difference")
    a = ap.parse_args()

    load_dotenv()
    url = os.getenv("DIRECT_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        print("db_fingerprint: set DIRECT_DATABASE_URL or DATABASE_URL", file=sys.stderr)
        return 2

    import psycopg

    if a.db:
        head, _, tail = url.rpartition("/")
        url = f"{head}/{a.db}{'?' + tail.split('?', 1)[1] if '?' in tail else ''}"

    with psycopg.connect(url) as conn:
        fp = fingerprint(conn)
        fp["database"] = conn.execute("SELECT current_database()").fetchone()[0]

    print(json.dumps(fp, indent=2))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(fp, indent=2), encoding="utf-8")

    if a.compare:
        want = json.loads(pathlib.Path(a.compare).read_text(encoding="utf-8"))
        diffs = []
        for table in sorted(set(want["tables"]) | set(fp["tables"])):
            b, n = want["tables"].get(table), fp["tables"].get(table)
            if b != n:
                diffs.append(f"  {table}: before={b} after={n}")
        if diffs:
            print(f"\nFINGERPRINT MISMATCH vs {a.compare} ({want.get('database')} -> {fp['database']}):")
            print("\n".join(diffs))
            return 1
        print(f"\nfingerprint MATCHES {a.compare} ({want.get('database')} -> {fp['database']}): "
              f"every table has the same row count, the same key digest and the same whole-row digest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
