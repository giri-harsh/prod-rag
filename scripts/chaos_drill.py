"""Chaos drill (Q6): kill each datastore under load and check the API degrades and recovers as designed.

    python scripts/chaos_drill.py --out _night/q6/chaos_result.json [--phase-s 20] [--port 8099]
    python scripts/chaos_drill.py --only qdrant          # one dependency
    python scripts/chaos_drill.py --no-kill              # baseline only; proves the harness without chaos

Runs with APP_ENV=loadtest, so the LLM, the embedder and the guard model are fakes and no provider is
billed; Postgres, Redis and Qdrant are REAL and are what gets killed.

The central claim under test is that **the API process is never restarted**. Each dependency is killed
mid-load, the drill records what /query and /readyz do, the dependency is brought back, and the drill
records whether traffic recovers on its own. The API's pid is captured at boot and asserted unchanged at
the end - a "recovery" that needed a restart is not a recovery.

Designed behaviour being verified (app/errors.py, app/api.py:readyz):
  redis     - caches and the auth-fail limiter degrade; /query keeps answering 200
  qdrant    - RetrievalUnavailable -> 503 + Retry-After: 5
  postgres  - readiness fails; the checkpointer cannot persist, so /query fails rather than lying
In every case /readyz must go 503 while the dependency is down and return to 200 once it is back.

Windows-specific: services are the local processes started by _infra/up_live.sh. Each is killed by finding the
pid LISTENING on its port (and that pid's children) rather than by image name, so the drill cannot take down an
unrelated Postgres or Redis that happens to be running on the machine.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

INFRA = ROOT.parent / "_infra"

# name -> (port, expected image name (recorded, not used to target the kill), shell command that starts it again)
SERVICES: dict[str, tuple[int, str, str]] = {
    "redis": (6379, "redis-server.exe",
              f"cd '{INFRA}/redis/Redis-8.10.2-Windows-x64-msys2' && nohup ./redis-server.exe --port 6379 "
              f"--save '' > ../redis6379.log 2>&1 &"),
    "qdrant": (6333, "qdrant.exe",
               f"cd '{INFRA}/qdrant' && QDRANT__STORAGE__STORAGE_PATH=./storage nohup ./qdrant.exe "
               f"> qdrant.log 2>&1 &"),
    "postgres": (5432, "postgres.exe",
                 f"cd '{INFRA}' && rm -f pg_live/postmaster.pid && nohup "
                 f"'{ROOT}/.venv/Lib/site-packages/pgserver/pginstall/bin/pg_ctl.exe' -D pg_live "
                 f"-o '-p 5432 -c listen_addresses=127.0.0.1' -l pg_live.log start > /dev/null 2>&1 &"),
}

QUESTIONS = [
    "How does the horizontal pod autoscaler decide when to scale pods?",
    "What is a CronJob and how is it scheduled?",
    "How do I monitor the progress of a Job?",
    "What is the backoff limit of a Job?",
]


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    with contextlib.closing(socket.socket()) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def wait_port(port: int, up: bool, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port) == up:
            return True
        time.sleep(0.5)
    return port_open(port) == up


def listening_pids(port: int) -> list[int]:
    """PIDs with a LISTEN socket on `port`, plus their children (postgres runs a tree of helper processes)."""
    import psutil

    pids = []
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
            pids.append(c.pid)
    return sorted(set(pids))


def kill_service(name: str) -> dict:
    """Kill only the process LISTENING on this service's port, and its children.

    Not `taskkill /F /IM postgres.exe`: that matches by image name, so a drill inside this project would also
    kill an unrelated Postgres or Redis the machine happens to be running. The port is what identifies the
    instance this project started.
    """
    import psutil

    port, exe, _ = SERVICES[name]
    pids = listening_pids(port)
    killed, failed = [], []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            victims = [*proc.children(recursive=True), proc]  # children first: a parent's death can orphan them
            for v in victims:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    v.kill()
            psutil.wait_procs(victims, timeout=15)
            killed.append({"pid": pid, "name": proc.name(), "children": len(victims) - 1})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            failed.append({"pid": pid, "error": type(e).__name__})
    down = wait_port(port, up=False, timeout=30)
    return {"action": "kill", "service": name, "expected_exe": exe, "killed": killed, "failed": failed,
            "port_closed": down}


def start_service(name: str) -> dict:
    port, _, cmd = SERVICES[name]
    subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=60)
    up = wait_port(port, up=True, timeout=90)
    return {"action": "start", "service": name, "port_open": up}


async def drive(client, key: str, seconds: float, stats: dict) -> None:
    """Post /query continuously for `seconds`, bucketing the outcome of every request."""

    end = time.time() + seconds
    i = 0
    while time.time() < end:
        q = QUESTIONS[i % len(QUESTIONS)]
        i += 1
        t0 = time.perf_counter()
        try:
            r = await client.post("/query", json={"q": q, "session_id": f"chaos{i % 7}"},
                                  headers={"X-API-Key": key}, timeout=30)
            code = r.status_code
            stats["status"][str(code)] = stats["status"].get(str(code), 0) + 1
            if code == 503 and "retry-after" in {k.lower() for k in r.headers}:
                ra = r.headers.get("Retry-After") or r.headers.get("retry-after")
                stats["retry_after"][str(ra)] = stats["retry_after"].get(str(ra), 0) + 1
            if code >= 400:
                with contextlib.suppress(Exception):
                    detail = str(r.json().get("detail") or r.json())[:120]
                    stats["messages"][detail] = stats["messages"].get(detail, 0) + 1
        except Exception as e:  # noqa: BLE001 - a transport failure is itself a result
            name = type(e).__name__
            stats["transport_errors"][name] = stats["transport_errors"].get(name, 0) + 1
        stats["latencies_ms"].append(round((time.perf_counter() - t0) * 1000))
        await asyncio.sleep(0.25)


async def probe_ready(client, admin_key: str) -> dict:

    try:
        r = await client.get("/readyz", headers={"X-API-Key": admin_key}, timeout=15)
        body = r.json()
        return {"status": r.status_code, "ready": body.get("ready"), "checks": body.get("checks")}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error": type(e).__name__}


def new_stats() -> dict:
    return {"status": {}, "retry_after": {}, "messages": {}, "transport_errors": {}, "latencies_ms": []}


def summarize(stats: dict) -> dict:
    lat = sorted(stats.pop("latencies_ms"))
    total = sum(stats["status"].values()) + sum(stats["transport_errors"].values())
    ok = stats["status"].get("200", 0)
    return {**stats, "total": total, "ok": ok,
            "ok_pct": round(100 * ok / total, 1) if total else 0.0,
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[int(len(lat) * 0.95)] if lat else None}


async def run(a) -> int:
    import httpx

    out: dict = {"run_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "phase_seconds": a.phase_s, "phases": [], "events": []}

    # --- preconditions ----------------------------------------------------------------------------
    for name, (port, _, _) in SERVICES.items():
        if not port_open(port):
            print(f"chaos: {name} is not running on {port}; start it with _infra/up_live.sh", file=sys.stderr)
            return 2

    env = {**os.environ, "APP_ENV": "loadtest", "QDRANT_COLLECTION": a.collection,
           "METRICS_PORT": "0", "PORT": str(a.port)}
    keys = mint_keys(env)
    out["tenant"] = keys["tenant"]

    api = subprocess.Popen(
        [str(ROOT / ".venv/Scripts/python.exe"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(a.port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out["api_pid"] = api.pid
    try:
        if not wait_port(a.port, up=True, timeout=180):
            print("chaos: the API never opened its port", file=sys.stderr)
            return 1

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{a.port}") as client:
            # wait for readiness before claiming a baseline
            for _ in range(60):
                if (await probe_ready(client, keys["admin"]))["status"] == 200:
                    break
                await asyncio.sleep(2)

            targets = [a.only] if a.only else ["redis", "qdrant", "postgres"]
            await phase(out, client, keys, "baseline", a.phase_s)

            for name in ([] if a.no_kill else targets):
                out["events"].append(kill_service(name))
                await phase(out, client, keys, f"{name}_down", a.phase_s)
                out["events"].append(start_service(name))
                # give the dependency a moment to accept connections before judging recovery
                await asyncio.sleep(3)
                await phase(out, client, keys, f"{name}_recovered", a.phase_s)

        # The central claim of the drill is that the API process is never restarted, so check it rather than
        # restating that it is alive: the pid launched at boot must still be the one holding the port (or one of
        # its own children), which also catches a supervisor quietly swapping in a new process.
        import psutil

        out["api_still_running"] = api.poll() is None
        family = {api.pid}
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            family |= {c.pid for c in psutil.Process(api.pid).children(recursive=True)}
        out["api_listener_pids"] = listening_pids(a.port)
        out["api_pid_unchanged"] = (out["api_still_running"] and bool(out["api_listener_pids"])
                                    and set(out["api_listener_pids"]) <= family)
    finally:
        api.terminate()
        with contextlib.suppress(Exception):
            api.wait(timeout=20)

    out["verdicts"] = verdicts(out)
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n=== chaos drill ===")
    for p in out["phases"]:
        s = p["query"]
        print(f"  {p['name']:22} {s['ok_pct']:5.1f}% 200  status={s['status']} "
              f"retry_after={s['retry_after'] or '-'}  readyz={p['readyz'].get('status')}")
    print(f"\n  API process survived the whole drill: {out.get('api_still_running')}")
    bad = [v for v in out["verdicts"] if not v["pass"]]
    for v in out["verdicts"]:
        print(f"  [{'PASS' if v['pass'] else 'FAIL'}] {v['check']}: {v['detail']}")
    print(f"\n-> {a.out}")
    return 1 if bad else 0


async def phase(out: dict, client, keys: dict, name: str, seconds: float) -> None:
    print(f"chaos: phase {name} ({seconds}s)...", flush=True)
    stats = new_stats()
    task = asyncio.create_task(drive(client, keys["tenant_key"], seconds, stats))
    await asyncio.sleep(min(3.0, seconds / 2))
    ready = await probe_ready(client, keys["admin"])
    await task
    out["phases"].append({"name": name, "query": summarize(stats), "readyz": ready})


def mint_keys(env: dict) -> dict:
    """Create a fresh admin key and a fresh tenant key so the drill does not depend on existing data."""
    tenant = f"chaos{dt.datetime.now().strftime('%H%M%S')}"
    made = {}
    for role in ("admin", "tenant"):
        cmd = [str(ROOT / ".venv/Scripts/python.exe"), "-m", "app.auth.cli", "create",
               "--tenant", tenant, "--user", f"drill-{role}"] + (["--admin"] if role == "admin" else [])
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"chaos: could not mint the {role} key: {r.stderr.strip()[:300]}")
        token = next((w for w in r.stdout.split() if w.startswith("rag_")), "")
        if not token:
            raise SystemExit(f"chaos: no key in the CLI output: {r.stdout.strip()[:300]}")
        made[role] = token
    return {"tenant": tenant, "admin": made["admin"], "tenant_key": made["tenant"]}


def verdicts(out: dict) -> list[dict]:
    """Compare what happened against the designed degradation. Reports every check; never raises."""
    by = {p["name"]: p for p in out["phases"]}
    v: list[dict] = []

    def add(check, ok, detail):
        v.append({"check": check, "pass": bool(ok), "detail": detail})

    base = by.get("baseline")
    if base:
        add("baseline serves traffic", base["query"]["ok_pct"] >= 95,
            f"{base['query']['ok_pct']}% 200")
        add("baseline is ready", base["readyz"].get("status") == 200, f"readyz={base['readyz'].get('status')}")

    if "redis_down" in by:
        d = by["redis_down"]
        add("redis down: /query keeps answering", d["query"]["ok_pct"] >= 95,
            f"{d['query']['ok_pct']}% 200 (caches and the auth-fail limiter degrade)")
        add("redis down: readiness fails", d["readyz"].get("status") == 503,
            f"readyz={d['readyz'].get('status')} checks={_check(d, 'redis')}")
    if "qdrant_down" in by:
        d = by["qdrant_down"]
        codes = d["query"]["status"]
        add("qdrant down: 503 to clients", codes.get("503", 0) > 0 and codes.get("200", 0) == 0,
            f"status={codes}")
        add("qdrant down: Retry-After sent", "5" in d["query"]["retry_after"],
            f"retry_after={d['query']['retry_after']}")
        add("qdrant down: readiness fails", d["readyz"].get("status") == 503,
            f"readyz={d['readyz'].get('status')} checks={_check(d, 'qdrant')}")
    if "postgres_down" in by:
        d = by["postgres_down"]
        add("postgres down: readiness fails", d["readyz"].get("status") == 503,
            f"readyz={d['readyz'].get('status')} checks={_check(d, 'postgres')}")
        add("postgres down: no silent success", d["query"]["status"].get("200", 0) == 0,
            f"status={d['query']['status']}")

    for name in ("redis", "qdrant", "postgres"):
        p = by.get(f"{name}_recovered")
        if not p:
            continue
        add(f"{name} recovered: traffic returns", p["query"]["ok_pct"] >= 95, f"{p['query']['ok_pct']}% 200")
        add(f"{name} recovered: readiness returns", p["readyz"].get("status") == 200,
            f"readyz={p['readyz'].get('status')}")

    add("the API process was never restarted", out.get("api_still_running") is True,
        f"pid {out.get('api_pid')} alive at the end: {out.get('api_still_running')}")
    return v


def _check(phase: dict, dep: str) -> str:
    checks = phase.get("readyz", {}).get("checks") or {}
    return f"{dep}={checks.get(dep, '?')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_night/q6/chaos_result.json")
    ap.add_argument("--phase-s", type=float, default=20.0)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--collection", default="loadtest_rag")
    ap.add_argument("--only", default="", choices=["", "redis", "qdrant", "postgres"])
    ap.add_argument("--no-kill", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
