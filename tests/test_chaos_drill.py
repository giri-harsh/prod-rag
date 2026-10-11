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


def test_start_service_does_not_block_on_a_service_that_inherits_its_pipes(monkeypatch, tmp_path):
    """Restarting a service must not hang the drill, even though the service keeps bash's fds open.

    This is the defect that made the first real drill unrecoverable: start_service used
    `subprocess.run(..., capture_output=True, timeout=60)`, the restarted msys2 service inherited bash's
    stdout/stderr pipe handles, and the pipes never reached EOF. `timeout=` did not save it - on Windows
    subprocess.run raises TimeoutExpired and then calls communicate() again with NO timeout, which blocked for
    the service's whole lifetime (measured: 218 s for a timeout=8 call). The kill had already succeeded, so the
    drill died holding the service down.

    The fake service here inherits the handles on purpose - it is backgrounded with no redirect of its own -
    and outlives the assertion by far, so this test hangs for ~120 s against the old implementation and
    finishes in about a second against the fixed one.
    """
    port = _free_port()
    script = tmp_path / "fake_service.py"
    script.write_text(
        "import socket, time\n"
        "s = socket.socket()\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen(8)\n"
        "time.sleep(120)\n", encoding="utf-8")
    exe = sys.executable.replace("\\", "/")
    # No `> log 2>&1` on the backgrounded process: it inherits whatever bash's stdout/stderr are.
    cmd = f"nohup '{exe}' '{str(script).replace(chr(92), '/')}' &"
    monkeypatch.setitem(drill.SERVICES, "fake", (port, pathlib.Path(sys.executable).name, cmd))

    t0 = time.perf_counter()
    ev = drill.start_service("fake", tmp_path)
    elapsed = time.perf_counter() - t0
    try:
        assert ev["port_open"] is True, f"the fake service never opened its port: {ev}"
        assert elapsed < 30, (f"start_service took {elapsed:.0f}s; it is waiting on pipes the restarted "
                              f"service holds open (the service sleeps 120s)")
        assert (tmp_path / "start_fake.log").exists(), "bash's output must land in a file, not a pipe"
    finally:
        for pid in drill.listening_pids(port):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def test_start_service_never_pipes_the_restart_command():
    """Text guard, like the /IM one: a pipe here is wrong by construction, not just slow.

    A refactor could reintroduce capture_output=True and still pass the timing test on a machine where the
    service happens not to inherit the handles, then hang on the machine that matters.
    """
    src = (ROOT / "scripts" / "chaos_drill.py").read_text(encoding="utf-8")
    code = re.sub(r'(?s)""".*?"""', "", src)
    code = "\n".join(line.partition("#")[0] for line in code.splitlines())
    body = code.split("def start_service")[1].split("\ndef ")[0]
    assert "capture_output" not in body, \
        "start_service pipes the restart command again; the service inherits the pipe and it never EOFs"
    assert "PIPE" not in body, "start_service must redirect the restart command to a file, not a pipe"


# --- verdicts ------------------------------------------------------------------------------------------

def _out(**kw) -> dict:
    base = {"api_pid": 4242, "api_still_running": True, "api_pid_unchanged": True,
            "api_listener_pids": [4242], "phases": [], "recover_budget_s": 60.0,
            "recovery": {"redis": 1.0, "qdrant": 2.0, "postgres": 6.1},
            "traffic_recovery": {"redis": 1.0, "qdrant": 12.0, "postgres": 7.0}}
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


def test_recovery_verdict_is_a_duration_against_a_budget_not_one_snapshot():
    """"readiness returns" must mean "returned within the budget", and must report how long it took.

    The first completed drill reported `postgres recovered: readiness returns` as a FAILURE while the
    application was healthy: the old check took a single readiness snapshot a flat 3 s after the restart, and
    psycopg's pool needs a few seconds to replace the connections the restart broke (measured: readiness came
    back at t+6.1 s). One sample cannot tell "never recovers" from "has not recovered yet", which is the
    difference between a production defect and a harness that measured too early.
    """
    def phase(name, readyz=200):
        return {"name": name, "query": {"status": {"200": 40}, "retry_after": {}, "ok_pct": 100.0},
                "readyz": {"status": readyz, "checks": {}}}

    # recovered, slowly but inside the budget -> PASS, and the measured time is reported
    slow = _out(phases=[phase("postgres_recovered")], recovery={"postgres": 6.1})
    v = _verdict(slow, "postgres recovered: readiness returns")
    assert v["pass"] is True, "a dependency that recovered in 6.1s of a 60s budget must pass"
    assert "6.1" in v["detail"], v

    # never came back inside the budget -> FAIL, and says so
    never = _out(phases=[phase("postgres_recovered", readyz=503)], recovery={"postgres": None})
    v = _verdict(never, "postgres recovered: readiness returns")
    assert v["pass"] is False
    assert "never returned" in v["detail"], v


def test_traffic_recovery_tolerates_the_breaker_cooldown_but_not_a_dead_service():
    """Traffic recovery is measured as time-to-first-200, not the ok_pct of a fixed-length phase.

    After Qdrant came back, /readyz pinged it fine in 0.1 s while /query kept returning 503 for 15 more
    requests: the circuit breaker only goes half-open breaker_reset_s (30 s) after it opened. The 20 s
    "recovered" phase sat inside that window, so the designed cooldown scored as "qdrant recovered: traffic
    returns - 40.0% 200", i.e. a load-shedding feature reported as a defect. The budget has to exceed
    breaker_reset_s, and a service that truly never serves again must still fail.
    """
    def phase(name, ok_pct, status):
        return {"name": name, "query": {"status": status, "retry_after": {}, "ok_pct": ok_pct},
                "readyz": {"status": 200, "checks": {}}}

    # recovered after the breaker cooldown, then steady -> PASS, with the measured delay reported
    slow = _out(phases=[phase("qdrant_recovered", 100.0, {"200": 30})], traffic_recovery={"qdrant": 12.0})
    v = _verdict(slow, "qdrant recovered: traffic returns")
    assert v["pass"] is True, "a breaker cooldown inside the budget is designed behaviour, not a deviation"
    assert "12.0" in v["detail"], v

    # never served again inside the budget -> FAIL
    dead = _out(phases=[phase("qdrant_recovered", 0.0, {"503": 30})], traffic_recovery={"qdrant": None})
    v = _verdict(dead, "qdrant recovered: traffic returns")
    assert v["pass"] is False
    assert "no 200 within" in v["detail"], v

    # recovered once, then went bad again during the phase -> FAIL (the phase still has to be healthy)
    flapping = _out(phases=[phase("qdrant_recovered", 50.0, {"200": 15, "503": 15})],
                    traffic_recovery={"qdrant": 2.0})
    assert _verdict(flapping, "qdrant recovered: traffic returns")["pass"] is False
