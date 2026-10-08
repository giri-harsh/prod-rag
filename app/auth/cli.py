"""Key management CLI.

  python -m app.auth.cli create --tenant acme --user alice [--admin] [--per-min 60] [--per-day 10000]
  python -m app.auth.cli revoke --key rag_xxx   |  --id <uuid>
  python -m app.auth.cli list
  python -m app.auth.cli tenant-set --tenant acme --domain "Kubernetes and networking"
  python -m app.auth.cli seed            # dev only: two demo tenants + an admin; prints raw keys once
"""
from __future__ import annotations

import argparse
import sys

from app.auth import keys
from app.config import load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.auth.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--tenant", required=True)
    c.add_argument("--user", required=True)
    c.add_argument("--admin", action="store_true")
    c.add_argument("--per-min", type=int)
    c.add_argument("--per-day", type=int)
    c.add_argument("--label")
    c.add_argument("--scope", action="append", default=[], help="e.g. eval (repeatable)")
    r = sub.add_parser("revoke")
    r.add_argument("--key")
    r.add_argument("--id")
    sub.add_parser("list")
    t = sub.add_parser("tenant-set", help="set a tenant's assistant domain description")
    t.add_argument("--tenant", required=True)
    t.add_argument("--domain", help="omit to reset to the global default")
    sub.add_parser("seed")
    a = ap.parse_args(argv)
    s = load_settings()
    db = s.database_url

    if a.cmd == "create":
        raw = keys.create_key(
            db, a.tenant, a.user, is_admin=a.admin, label=a.label, scopes=tuple(a.scope), redis_url=s.redis_url,
            rate_per_min=a.per_min or s.default_rate_per_min, rate_per_day=a.per_day or s.default_rate_per_day,
        )
        print(raw)  # the only time the raw key is ever visible
    elif a.cmd == "revoke":
        n = keys.revoke_key(db, raw=a.key, key_id=a.id, redis_url=s.redis_url)
        print(f"revoked {n} key(s)")
        return 0 if n else 1
    elif a.cmd == "tenant-set":
        keys.set_tenant_domain(db, a.tenant, a.domain)
        print("ok")
    elif a.cmd == "list":
        for row in keys.list_keys(db):
            print(*row, sep="\t")
    elif a.cmd == "seed":
        if s.app_env == "prod":
            print("refusing to seed in prod", file=sys.stderr)
            return 2
        for tenant, user, admin in (("tenant-a", "alice", False), ("tenant-b", "bob", False), ("tenant-a", "root", True)):
            print(f"{tenant}/{user}{' (admin)' if admin else ''}: {keys.create_key(db, tenant, user, is_admin=admin, label='seed')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
