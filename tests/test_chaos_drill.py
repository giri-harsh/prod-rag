"""Tests for the chaos drill harness itself (scripts/chaos_drill.py).

The drill is the thing that kills real services, so its two dangerous properties are tested here rather than
discovered during a run:

1. It must kill the process LISTENING on the service port - never every process sharing an image name. The
   first version ran `taskkill /F /IM postgres.exe`, which would have killed an unrelated Postgres the machine
   happened to be running. test_kill_service_kills_only_the_port_owner spawns two identical child processes
   (same executable) and proves only the listener dies.
2. Its verdicts must not pass on a weaker claim than the one they state. "The API process was never restarted"
   was asserted from `api.poll() is None`, which is true of any live process, including a replacement.

These run no LLM and touch no project service: the fake "service" is a throwaway python child on a free port.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import re
import socket
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent


def _load_drill():
    """Import scripts/chaos_drill.py by path (scripts/ is not a package).

    The module sets the Windows selector event loop policy at import time; it is saved and restored so importing
    it cannot change the policy the rest of the test session runs under.
    """
    policy = asyncio.get_event_loop_policy()
    spec = importlib.util.spec_from_file_location("chaos_drill", ROOT / "scripts" / "chaos_drill.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        asyncio.set_event_loop_policy(policy)
    return mod


drill = _load_drill()
psutil = pytest.importorskip("psutil")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_listener(port: int | None) -> subprocess.Popen:
    """A child process that holds a listening socket on `port`, or just sleeps when port is None."""
    code = (f"import socket,time;s=socket.socket();s.bind(('127.0.0.1',{port}));s.listen(8);time.sleep(120)"
            if port else "import time;time.sleep(120)")
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_the_drill_never_kills_by_image_name():
    """A regression guard on the text of the kill, not just its behaviour: `taskkill /F /IM <exe>` is
    indiscriminate by construction, so it must not come back even in a refactor that still passes the
    behavioural tests below (e.g. as a fallback path when no listening pid is found).

    Comments and docstrings are stripped first - they explain the old behaviour on purpose, and matching them
    would make the guard fire on its own documentation.
    """
    src = (ROOT / "scripts" / "chaos_drill.py").read_text(encoding="utf-8")
    code = re.sub(r'(?s)""".*?"""', "", src)
    code = "\n".join(line.partition("#")[0] for line in code.splitlines())
    assert "/IM" not in code, "chaos_drill kills by image name again; that hits processes outside this project"
    assert "taskkill" not in code.lower(), "chaos_drill must resolve the listening pid, not shell out to taskkill"


def test_listening_pids_finds_the_owning_process():
    """The owner is the pid holding the socket, which is not necessarily the pid Popen returned.

    `.venv/Scripts/python.exe` is a launcher that re-execs the real interpreter as a child, so the LISTEN socket
    belongs to a descendant. listening_pids must name whoever actually holds the port.
    """
    port = _free_port()
    child = _spawn_listener(port)
    try:
        assert drill.wait_port(port, up=True, timeout=30), "the fake service never opened its port"
        owners = drill.listening_pids(port)
        assert len(owners) == 1, owners
        assert owners[0] in drill.api_family(child.pid), \
            f"the port owner {owners} is not the spawned process or one of its children"
        assert drill.listening_pids(_free_port()) == [], "a port nobody listens on has no owner"
    finally:
        child.kill()
        child.wait(timeout=30)


def test_kill_service_kills_only_the_port_owner(monkeypatch):
    """Two children of the SAME executable; only the one holding the port may die."""
    port = _free_port()
    listener = _spawn_listener(port)
    bystander = _spawn_listener(None)  # same image name, no socket - must survive
    monkeypatch.setitem(drill.SERVICES, "fake", (port, pathlib.Path(sys.executable).name, "true"))
    try:
        assert drill.wait_port(port, up=True, timeout=30)
        owner = drill.listening_pids(port)[0]
        bystander_family = drill.api_family(bystander.pid)
        ev = drill.kill_service("fake")

        assert ev["port_closed"] is True, f"the port stayed open: {ev}"
        assert [k["pid"] for k in ev["killed"]] == [owner], ev
        assert ev["failed"] == [], ev
        assert _dead(owner), "the port owner survived the kill"
        assert all(psutil.pid_exists(pid) for pid in bystander_family), \
            "a process that merely shares the executable name was killed"
    finally:
        for p in (listener, bystander):
            for pid in drill.api_family(p.pid):
                try:  # best-effort cleanup; the listener's family is already gone
                    psutil.Process(pid).kill()
                except psutil.Error:
                    pass
            p.wait(timeout=30)


def test_stop_api_leaves_nothing_holding_the_port():
    """The drill must not orphan the real server when it exits.

    Popen.terminate() hits only the launcher on Windows, so the uvicorn process it spawned kept the port, the
    Postgres pool and the Redis connections alive after the drill finished - and the next run would then fail
    to bind and blame itself.
    """
    port = _free_port()
    api = _spawn_listener(port)
    family = None
    try:
        assert drill.wait_port(port, up=True, timeout=30)
        family = drill.api_family(api.pid)
        assert len(family) >= 1
        drill.stop_api(api, port)
        assert all(_dead(pid) for pid in family), f"survivors in {family}"
        assert not drill.port_open(port), "the port is still held after stop_api"
        assert drill.listening_pids(port) == []
    finally:
        for pid in family or [api.pid]:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def _dead(pid: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.2)
    return not psutil.pid_exists(pid)


def test_kill_service_reports_a_port_it_could_not_close(monkeypatch):
    """When the owning pid cannot be resolved, the kill must report port_closed=False.

    run() turns that into an abort. Reporting a no-op as a successful kill is what would produce phantom "the
    app did not degrade" verdicts: the dependency keeps serving right through the `*_down` phase, so every
    degradation check fails and the harness's blind spot reads as an application defect.
    """
    port = _free_port()
    child = _spawn_listener(port)
    monkeypatch.setitem(drill.SERVICES, "fake", (port, "python.exe", "true"))
    monkeypatch.setattr(drill, "listening_pids", lambda _p: [])  # e.g. psutil cannot see the owner
    orig_wait = drill.wait_port
    try:
        assert orig_wait(port, up=True, timeout=30)
        # kill_service hardcodes a 30 s wait for the port to close; it never will here, so cap the wait
        monkeypatch.setattr(drill, "wait_port", lambda p, up, timeout=30: orig_wait(p, up, min(timeout, 2)))
        ev = drill.kill_service("fake")
        assert ev["killed"] == [] and ev["port_closed"] is False, ev
        assert child.poll() is None, "nothing should have been killed"
    finally:
        child.kill()
        child.wait(timeout=30)


# --- verdicts ------------------------------------------------------------------------------------------

def _out(**kw) -> dict:
    base = {"api_pid": 4242, "api_still_running": True, "api_pid_unchanged": True,
            "api_listener_pids": [4242], "phases": []}
    return {**base, **kw}


def _verdict(out: dict, check: str) -> dict:
    return next(v for v in drill.verdicts(out) if v["check"] == check)


RESTART_CHECK = "the API process was never restarted"


def test_restart_verdict_passes_when_the_booted_pid_still_serves():
    assert _verdict(_out(), RESTART_CHECK)["pass"] is True


def test_restart_verdict_fails_when_another_pid_took_over_the_port():
    """The regression: alive-but-replaced used to PASS, because the check was only `poll() is None`."""
    out = _out(api_pid_unchanged=False, api_listener_pids=[9999])
    v = _verdict(out, RESTART_CHECK)
    assert v["pass"] is False, "a replacement process holding the port must not pass as 'never restarted'"
    assert "9999" in v["detail"], v


def test_restart_verdict_fails_when_the_api_died():
    out = _out(api_still_running=False, api_pid_unchanged=False, api_listener_pids=[])
    assert _verdict(out, RESTART_CHECK)["pass"] is False


def test_degradation_verdicts_match_the_documented_design():
    """redis down keeps serving 200; qdrant down must 503 with Retry-After; postgres down must not fake success."""
    def phase(name, status, retry_after=None, readyz=200, ok_pct=100.0):
        return {"name": name, "query": {"status": status, "retry_after": retry_after or {}, "ok_pct": ok_pct},
                "readyz": {"status": readyz, "checks": {}}}

    out = _out(phases=[
        phase("baseline", {"200": 40}),
        phase("redis_down", {"200": 40}, readyz=503),
        phase("redis_recovered", {"200": 40}),
        phase("qdrant_down", {"503": 40}, retry_after={"5": 40}, readyz=503, ok_pct=0.0),
        phase("qdrant_recovered", {"200": 40}),
        phase("postgres_down", {"503": 40}, readyz=503, ok_pct=0.0),
        phase("postgres_recovered", {"200": 40}),
    ])
    assert [v for v in drill.verdicts(out) if not v["pass"]] == []

    # negative controls: each designed behaviour, violated one at a time
    broke_redis = _out(phases=[phase("redis_down", {"503": 40}, readyz=503, ok_pct=0.0)])
    assert _verdict(broke_redis, "redis down: /query keeps answering")["pass"] is False

    no_retry_after = _out(phases=[phase("qdrant_down", {"503": 40}, readyz=503, ok_pct=0.0)])
    assert _verdict(no_retry_after, "qdrant down: Retry-After sent")["pass"] is False

    silent_success = _out(phases=[phase("postgres_down", {"200": 40}, readyz=503)])
    assert _verdict(silent_success, "postgres down: no silent success")["pass"] is False

    still_ready = _out(phases=[phase("qdrant_down", {"503": 40}, retry_after={"5": 1}, readyz=200, ok_pct=0.0)])
    assert _verdict(still_ready, "qdrant down: readiness fails")["pass"] is False
