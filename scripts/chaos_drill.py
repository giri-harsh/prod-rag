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
            # Read the identity BEFORE killing: proc.name() on a dead process raises NoSuchProcess, which this
            # very except clause then filed under "failed" - so a kill that worked (port closed, service down)
            # was reported as having killed nothing.
            record = {"pid": pid, "name": proc.name(), "children": len(victims) - 1}
            for v in victims:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    v.kill()
            gone, alive = psutil.wait_procs(victims, timeout=15)
            record["survivors"] = [p.pid for p in alive]
            (failed if alive else killed).append(record)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            failed.append({"pid": pid, "error": type(e).__name__})
    down = wait_port(port, up=False, timeout=30)
    return {"action": "kill", "service": name, "expected_exe": exe, "killed": killed, "failed": failed,
            "port_closed": down}


def api_family(pid: int) -> list[int]:
    """The API's pid plus its descendants.

    `.venv/Scripts/python.exe` is a launcher: it re-execs the real interpreter as a CHILD, so the process that
    actually holds the API port is not the pid Popen returned (measured on this machine - Popen pid 19204,
    LISTEN socket owned by 19512). Anything reasoning about "the API process" has to mean the whole tree.
    """
    import psutil

    out = [pid]
    with contextlib.suppress(Exception):
        out += [c.pid for c in psutil.Process(pid).children(recursive=True)]
    return out


def stop_api(api: subprocess.Popen, port: int) -> None:
    """Tear the API down by tree, not just the launcher.

    `api.terminate()` on Windows is TerminateProcess on the launcher only, which ORPHANS the real uvicorn
    process: it keeps the port, the Postgres pool and the Redis connections after the drill exits, so the next
    run finds port 8099 taken and blames it on itself.
    """
    import psutil

    pids = api_family(api.pid)
    procs = []
    for pid in pids:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            procs.append(psutil.Process(pid))
    for p in reversed(procs):  # children first
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=15)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            p.kill()
    psutil.wait_procs(alive, timeout=10)
    with contextlib.suppress(Exception):
        api.wait(timeout=10)
    if not wait_port(port, up=False, timeout=15):
        print(f"chaos: warning - something is still listening on {port} after shutdown", file=sys.stderr)


def start_service(name: str, log_dir: pathlib.Path) -> dict:
    """Restart a service and wait for its port. bash's output goes to a FILE, never to a pipe.

    This used to be `subprocess.run(..., capture_output=True, timeout=60)` and it hung the first real drill
    FOREVER on the very first restart - after the kill had already succeeded, so the run was unrecoverable.

    Why a pipe cannot be used here: the command backgrounds an msys2-built service binary, which inherits
    bash's stdout/stderr handles even though its own fds are redirected to the service's log. bash exits, the
    service keeps the write end open for its whole lifetime, and the pipe therefore never reaches EOF.

    And `timeout=` is not a safety net for this on Windows: subprocess.run raises TimeoutExpired and then, on
    the _mswindows branch, calls communicate() A SECOND TIME with no timeout to drain the pipes - which blocks
    for as long as the service lives. Measured on this machine: a timeout=8 call returned after 218 s, and only
    because the service was killed by hand.

    Writing to a file means there are no pipes, so no reader threads and nothing to drain; the inherited file
    handle is harmless. The log is kept and reported when the port does not come up, which is the only case
    where bash's own output matters.
    """
    port, _, cmd = SERVICES[name]
    log_dir = pathlib.Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"start_{name}.log"
    with log.open("w", encoding="utf-8") as fh:
        rc = subprocess.run(["bash", "-lc", cmd], stdout=fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, timeout=60).returncode
    up = wait_port(port, up=True, timeout=90)
    ev = {"action": "start", "service": name, "port_open": up, "rc": rc}
    if not up:
        ev["start_log"] = log.read_text(encoding="utf-8", errors="replace")[-800:]
    return ev


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


async def wait_ready(client, admin_key: str, budget_s: float, interval: float = 2.0) -> tuple[float | None, dict]:
    """Poll /readyz until it reports 200. Returns (seconds_to_ready or None if the budget ran out, last probe).

    Recovery is a duration, not an instant. The drill used to sleep a flat 3 s after restarting a dependency
    and then take ONE readiness snapshot, which reported "postgres recovered: readiness returns" as a FAILURE
    while the app was in fact fine: psycopg's pool needs a few seconds to replace the connections the restart
    broke, and measurement at t+3 s simply landed inside that window (measured separately: readiness came back
    at t+6.1 s). Sampling once cannot tell "never recovers" apart from "had not recovered yet", so the drill
    now measures how long recovery takes and judges that against a budget.
    """
    t0 = time.perf_counter()
    last: dict = {}
    while True:
        last = await probe_ready(client, admin_key)
        elapsed = time.perf_counter() - t0
        if last["status"] == 200:
            return round(elapsed, 1), last
        if elapsed >= budget_s:
            return None, last
        await asyncio.sleep(interval)


async def wait_serving(client, key: str, budget_s: float, interval: float = 1.0) -> tuple[float | None, int | None]:
    """Poll /query until it answers 200. Returns (seconds to the first 200 or None, last status code).

    Readiness coming back does not mean traffic is being served. The circuit breaker (app/resilience.py) opens
    after `breaker_failures` consecutive failures and only goes half-open `breaker_reset_s` (30 s) after it
    opened, then admits a single probe - so after Qdrant returns, /query keeps shedding load for the remainder
    of that window even though /readyz pings Qdrant successfully in 0.1 s. Measured: 15 further 503s before the
    first 200. That is the breaker working as designed, but a fixed-length "recovered" phase shorter than the
    reset window can only ever see it as "traffic did not return".
    """
    t0 = time.perf_counter()
    code = None
    while True:
        try:
            r = await client.post("/query", json={"q": QUESTIONS[0], "session_id": "chaos-recovery"},
                                  headers={"X-API-Key": key}, timeout=30)
            code = r.status_code
            if code == 200:
                return round(time.perf_counter() - t0, 1), code
        except Exception:  # noqa: BLE001 - a transport failure is just "not serving yet"
            code = None
        if time.perf_counter() - t0 >= budget_s:
            return None, code
        await asyncio.sleep(interval)


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
                 "phase_seconds": a.phase_s, "recover_budget_s": a.recover_budget_s,
                 "phases": [], "events": [], "recovery": {}, "traffic_recovery": {}}

    # --- preconditions ----------------------------------------------------------------------------
    for name, (port, _, _) in SERVICES.items():
        if not port_open(port):
            print(f"chaos: {name} is not running on {port}; start it with _infra/up_live.sh", file=sys.stderr)
            return 2

    # Refuse to start if something already holds the API port. uvicorn's bind failure goes to DEVNULL here, so
    # wait_port() would see the port open, the drill would drive traffic against the OTHER process and judge the
    # app by it. Two drills were accidentally run at once and did exactly that.
    if port_open(a.port):
        print(f"chaos: port {a.port} is already in use - another drill or API is running. Stop it, or pass "
              f"--port. Refusing to measure a process this drill did not start.", file=sys.stderr)
        return 2

    env = {**os.environ, "APP_ENV": "loadtest", "QDRANT_COLLECTION": a.collection,
           "METRICS_PORT": "0", "PORT": str(a.port)}
    keys = mint_keys(env)
    out["tenant"] = keys["tenant"]

    # `python -m app.main`, not `python -m uvicorn app.main:app`: on Windows the latter creates a
    # ProactorEventLoop before importing the app, psycopg's pool cannot use it, and the API then serves while
    # reporting postgres down - the first attempt at this drill hung for 17 minutes on exactly that.
    api_log = pathlib.Path(a.out).parent / "api.log"
    api_log.parent.mkdir(parents=True, exist_ok=True)
    # The API's own output goes to a file rather than DEVNULL. It was discarded, which made the failure above
    # invisible: the only evidence was a drill that never printed anything.
    with api_log.open("w", encoding="utf-8") as log:
        api = subprocess.Popen(
            [str(ROOT / ".venv/Scripts/python.exe"), "-m", "app.main"],
            cwd=str(ROOT), env={**env, "HOST": "127.0.0.1", "UVICORN_LOG_LEVEL": "warning"},
            stdout=log, stderr=subprocess.STDOUT)
    out["api_pid"] = api.pid
    out["api_log"] = str(api_log)
    try:
        if not wait_port(a.port, up=True, timeout=180):
            print("chaos: the API never opened its port", file=sys.stderr)
            return 1

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{a.port}") as client:
            # Wait for readiness before claiming a baseline - and refuse to drill an API that never became
            # ready. Carrying on regardless turns one startup problem into a wall of failed verdicts that look
            # like degradation defects.
            ready = None
            for _ in range(30):
                ready = await probe_ready(client, keys["admin"])
                if ready["status"] == 200:
                    break
                await asyncio.sleep(2)
            if not ready or ready["status"] != 200:
                print(f"chaos: the API never became ready (last probe: {ready}). See {api_log} for why; "
                      f"nothing was killed.", file=sys.stderr)
                out["aborted"] = {"why": "API never became ready", "last_probe": ready}
                pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
                return 2

            targets = [a.only] if a.only else ["redis", "qdrant", "postgres"]
            await phase(out, client, keys, "baseline", a.phase_s)

            for name in ([] if a.no_kill else targets):
                ev = kill_service(name)
                out["events"].append(ev)
                if not ev["port_closed"]:
                    # The dependency is still serving, so a "{name}_down" phase would measure a healthy system
                    # and every degradation verdict would FAIL - reading as an app deviation when the harness is
                    # what broke (e.g. psutil could not see the owning pid, so nothing was killed).
                    out["aborted"] = {"service": name, "why": "could not close the port; nothing was measured",
                                      "event": ev}
                    print(f"chaos: {name} still listening on {SERVICES[name][0]} after the kill "
                          f"(killed={ev['killed']} failed={ev['failed']}); aborting rather than reporting "
                          f"phantom deviations", file=sys.stderr)
                    return 2
                await phase(out, client, keys, f"{name}_down", a.phase_s)
                restart = start_service(name, api_log.parent)
                out["events"].append(restart)
                if not restart["port_open"]:
                    # Recovery cannot be measured if the dependency never came back, and the remaining
                    # services would be killed against an already-broken system.
                    out["aborted"] = {"service": name, "why": "did not come back up after the kill",
                                      "event": restart}
                    print(f"chaos: {name} did not come back on {SERVICES[name][0]} after the restart "
                          f"(see {api_log.parent / f'start_{name}.log'}); aborting", file=sys.stderr)
                    return 2
                # Measure how long the app takes to call itself ready again, rather than sleeping a flat 3s
                # and hoping that was long enough (it was not, for postgres).
                secs, last = await wait_ready(client, keys["admin"], a.recover_budget_s)
                out["recovery"][name] = secs
                # Then wait for traffic, separately: readiness and "actually serving" recover at different
                # times, because the breaker keeps shedding load after the dependency itself is healthy.
                served, code = await wait_serving(client, keys["tenant_key"], a.recover_budget_s)
                out["traffic_recovery"][name] = served
                out["events"].append({"action": "await_ready", "service": name, "ready_after_s": secs,
                                      "first_200_after_s": served, "last_status": code,
                                      "budget_s": a.recover_budget_s, "last_probe": last})
                print(f"chaos: {name} readiness back after {secs}s, first 200 after {served}s", flush=True)
                await phase(out, client, keys, f"{name}_recovered", a.phase_s)

        # The central claim of the drill is that the API process is never restarted, so check it rather than
        # restating that it is alive: the pid launched at boot must still be the one holding the port (or one of
        # its own children), which also catches a supervisor quietly swapping in a new process.
        out["api_still_running"] = api.poll() is None
        out["api_family_pids"] = api_family(api.pid)
        out["api_listener_pids"] = listening_pids(a.port)
        out["api_pid_unchanged"] = (out["api_still_running"] and bool(out["api_listener_pids"])
                                    and set(out["api_listener_pids"]) <= set(out["api_family_pids"]))
    finally:
        stop_api(api, a.port)

    out["verdicts"] = verdicts(out)
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n=== chaos drill ===")
    for p in out["phases"]:
        s = p["query"]
        print(f"  {p['name']:22} {s['ok_pct']:5.1f}% 200  status={s['status']} "
              f"retry_after={s['retry_after'] or '-'}  readyz={p['readyz'].get('status')}")
    if out.get("recovery"):
        print(f"\n  recovery after each restart (budget {a.recover_budget_s}s):")
        print(f"    {'service':10} {'/readyz 200':>12} {'first /query 200':>18}")
        for name, secs in out["recovery"].items():
            served = out["traffic_recovery"].get(name)
            fmt = (lambda v: f"{v}s" if v is not None else "never")  # noqa: E731 - local formatting shorthand
            print(f"    {name:10} {fmt(secs):>12} {fmt(served):>18}")
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
        # Traffic recovery is a duration too, and a different one from readiness: the breaker keeps shedding
        # load for the rest of its reset window after the dependency is healthy again. Judging only the ok_pct
        # of a fixed 20 s phase made Qdrant's designed breaker cooldown look like a failure to recover.
        served = (out.get("traffic_recovery") or {}).get(name)
        add(f"{name} recovered: traffic returns",
            served is not None and p["query"]["ok_pct"] >= 95,
            f"first 200 after {served}s, then {p['query']['ok_pct']}% 200" if served is not None
            else f"no 200 within {out.get('recover_budget_s')}s; phase was {p['query']['ok_pct']}% 200")
        # Judged as a duration against a budget. A single snapshot could not distinguish "never recovers" from
        # "has not recovered yet", and reported the app as broken for being 3 s short of ready.
        budget = out.get("recover_budget_s")
        secs = (out.get("recovery") or {}).get(name)
        add(f"{name} recovered: readiness returns",
            secs is not None and p["readyz"].get("status") == 200,
            f"readyz returned to 200 after {secs}s (budget {budget}s)" if secs is not None
            else f"readiness never returned within {budget}s; last readyz={p['readyz'].get('status')}")

    # "alive at the end" is true of any process; the claim is that the pid booted before the chaos is still the
    # one serving, so the check is api_pid_unchanged (booted pid or its own children still hold the port).
    add("the API process was never restarted", out.get("api_pid_unchanged") is True,
        f"booted pid {out.get('api_pid')}, alive={out.get('api_still_running')}, "
        f"listeners={out.get('api_listener_pids')}")
    return v


def _check(phase: dict, dep: str) -> str:
    """The per-dependency readiness detail, or why there is none.

    An empty `checks` is expected while Postgres is down: the detail is only shown to admin keys, that lookup
    is a Postgres read, and it is deliberately bounded and skipped rather than allowed to hang the endpoint
    (app/api.py:readyz). Printing a bare "?" made correct behaviour look like missing data.
    """
    body = phase.get("readyz", {})
    checks = body.get("checks") or {}
    if not checks:
        return f"{dep}=<no detail: the admin lookup reads postgres and is bounded>, ready={body.get('ready')}"
    return f"{dep}={checks.get(dep, '?')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_night/q6/chaos_result.json")
    ap.add_argument("--phase-s", type=float, default=20.0)
    ap.add_argument("--recover-budget-s", type=float, default=60.0,
                    help="how long a dependency's recovery may take before readiness returning counts as failed")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--collection", default="loadtest_rag")
    ap.add_argument("--only", default="", choices=["", "redis", "qdrant", "postgres"])
    ap.add_argument("--no-kill", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
