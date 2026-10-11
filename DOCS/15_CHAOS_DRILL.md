# 15 - Chaos drill: killing Redis, Qdrant and Postgres under load

What this covers: `scripts/chaos_drill.py`, which kills each datastore in turn while the API is taking traffic
and checks that the service degrades the way this project claims it does and recovers **without the API being
restarted**. The numbers below are from a real run against the real local services:
[`evals/chaos_drill_2026-10-11.json`](../evals/chaos_drill_2026-10-11.json).

```bash
# Postgres, Redis and Qdrant must be up first (_infra/up_live.sh)
PYTHONPATH=. python scripts/chaos_drill.py --out _night/q6/chaos_result.json --phase-s 20
PYTHONPATH=. python scripts/chaos_drill.py --only qdrant     # one dependency
PYTHONPATH=. python scripts/chaos_drill.py --no-kill         # prove the harness without any chaos
```

**Honesty note.** This is a single-node drill on Windows: one Redis, one Qdrant, one Postgres, all local
processes, killed with `SIGKILL`-equivalent (`Process.kill()`). It exercises *this application's* behaviour
when a dependency disappears. It does **not** exercise a Patroni/CloudNativePG failover, a network partition,
a slow-but-alive dependency, disk exhaustion, or multi-replica behaviour behind a load balancer. The LLM, the
embedder and the guard model are the `APP_ENV=loadtest` fakes, so the drill costs no provider budget and the
latencies below are *infrastructure* latencies with no model time in them. Nothing here was hand-written:
every number is read back out of the JSON the run produced.

## 1. Designed degradation, and what actually happened

| dependency | designed behaviour | measured |
|---|---|---|
| **Redis** | caches and the auth-fail limiter degrade; `/query` keeps answering 200; readiness fails | 100.0% 200 (9/9), `readyz` 503 with `redis=fail: TimeoutError` |
| **Qdrant** | retrieval is unavailable -> 503 + `Retry-After: 5`; readiness fails | 15/15 503, all with `Retry-After: 5`, `readyz` 503 with `qdrant=fail: ResponseHandlingException` |
| **Postgres** | readiness fails; no silent success - the request fails rather than lying | 5/5 503 + `Retry-After: 5`, `readyz` 503, `ready=false` |
| **all three** | the API process is never restarted | booted pid still owns the port at the end of the run |

16 of 16 verdicts pass. The full verdict list is in the artifact under `verdicts`.

## 2. Latency and throughput per phase

Each phase is 20 s of continuous `/query` at ~4 req/s.

| phase | requests | p50 | p95 | statuses |
|---|---|---|---|---|
| baseline | 29 | 386 ms | 718 ms | `200` x29 |
| redis_down | 9 | 2,035 ms | 2,406 ms | `200` x9 |
| redis_recovered | 29 | 372 ms | 832 ms | `200` x29 |
| qdrant_down | 15 | 452 ms | 2,788 ms | `503` x15, `Retry-After: 5` |
| qdrant_recovered | 28 | 410 ms | 799 ms | `200` x28 |
| postgres_down | 5 | 5,017 ms | 5,025 ms | `503` x5, `Retry-After: 5` |
| postgres_recovered | 34 | 309 ms | 598 ms | `200` x34 |

Redis down is the one phase that still serves traffic, and it costs ~5x baseline latency - see §4.1 for why
and how far that was reduced.

## 3. Recovery is a duration, not an instant

Readiness and "actually serving traffic" come back at **different** times, so the drill measures both against
a budget (`--recover-budget-s`, default 60 s) instead of taking one snapshot:

| dependency | `/readyz` back to 200 | first `/query` 200 | why they differ |
|---|---|---|---|
| redis | 0.1 s | 0.7 s | nothing to rebuild |
| qdrant | 0.0 s | **13.8 s** | the circuit breaker is still open (see below) |
| postgres | 8.6 s | 0.6 s | psycopg's pool has to replace the connections the restart killed; `/query` paths that do not need a fresh pooled connection recover first |

**Qdrant's 13.8 s is the circuit breaker working as designed, not a fault.** `app/resilience.py` opens the
breaker after `breaker_failures` (5) consecutive failures and only moves to half-open `breaker_reset_s` (30 s)
after it opened, then admits a single probe. So after Qdrant is healthy again, `/query` keeps shedding load
for the remainder of that window while `/readyz` - which pings Qdrant directly - already reports ready.

Two consequences worth being explicit about:

* **Readiness deliberately ignores breaker state.** A replica reports ready while it is still shedding load.
  That is the lesser evil: making readiness follow the breakers would take *every* replica out of the Service
  endpoints at the same moment, turning a degraded dependency into a total outage with no capacity to recover
  into. The `rag_circuit_breaker_open` alert in [14_SLOS_AND_RUNBOOKS.md](14_SLOS_AND_RUNBOOKS.md) is what
  covers this window.
* **A "recovered" phase shorter than `breaker_reset_s` cannot observe recovery.** This is what made the drill
  initially report `qdrant recovered: traffic returns - 40.0% 200` as a failure (§4.2).

## 4. Deviations found, and what was done about them

### 4.1 Redis down cost 16.4 s per request (fixed)

`/query` kept answering 200 with Redis dead - but p50 went **342 ms -> 16,377 ms** and only 2 requests
finished in a 20 s phase. "Degrades gracefully" is not true at 16 s a request; any real client has already
timed out.

The client was built as `aioredis.from_url(url)`, which leaves `socket_timeout` and `socket_connect_timeout`
at `None`. Measured on a throwaway Redis, per-call cost against a *dead* Redis is exactly the connect timeout
(a connect to a dead local Redis does **not** fail fast in redis-py's async path):

| `socket_connect_timeout` | 8 sequential GETs | per call |
|---|---|---|
| `None` (the old default) | 14,124 ms | ~1,766 ms |
| 1.0 s | 7,063 ms | 883 ms |
| 0.5 s | 4,023 ms | 503 ms |
| 0.2 s | 1,690 ms | 211 ms |
| 0.05 s | 518 ms | 65 ms |

The configured default is **0.25 s**, between the two bottom rows: a cache miss is cheap, a 16 s request is
not, and a too-tight connect timeout only ever causes more fallback misses - never a wrong answer. Raise it if
Redis is genuinely far away (cross-region).

~8 calls is what one `/query` makes, because each consumer degrades independently and therefore fails
independently: planner cache, rate limiter, auth-fail limiter, key negative-cache, embedding cache, thread
lock. `redis_connect_timeout_s` (0.25 s) and `redis_socket_timeout_s` (2.0 s) are now explicit and
configurable; the measured effect in the drill was **p50 16,377 ms -> 2,035 ms** and 2 -> 9 requests per phase.

Two notes. With no timeouts at all, a Redis host that is *unreachable* rather than *refusing* would have
blocked a request indefinitely - that hazard was the more serious half of this. And the residual ~2 s is still
N_calls x connect_timeout; eliminating it properly means short-circuiting Redis while it is known-down, i.e.
putting the shared client behind the `CircuitBreaker` this project already has. That is **not done** - it is a
behaviour change across eight call sites with different exception-handling, and `CircuitOpen` is not a
`redis.RedisError`, so it needs each consumer's fallback audited rather than guessed at. Left open and
recorded here deliberately.

### 4.2 Postgres down returned 500 after 30 s (fixed)

`psycopg_pool.PoolTimeout` escaped unhandled, so a Postgres outage answered a generic
`500 internal server error` with no `Retry-After` - while the equivalent Qdrant outage answered `503` +
`Retry-After: 5` in under a second. Same class of event, two different contracts.

* `DatabaseUnavailable` (503, `Retry-After: 5`) added, and a handler keyed on `psycopg.OperationalError`,
  which `PoolTimeout` subclasses - so it covers both "the pool cannot hand out a connection" and "a connection
  dropped mid-query".
* The pool was using psycopg's default 30 s queue timeout, which is where the 30 s went.
  `pg_pool_timeout_s` (5 s) now bounds it: nothing useful happens in that window, and the 503 is more useful
  immediately. It also caps the queue wait when the pool is merely saturated, so raise it or `pg_pool_max` if
  legitimate load is being shed rather than queued.

Measured: `postgres_down` went from `500` at p50 **30,007 ms** to `503` + `Retry-After: 5` at p50 **5,017 ms**.

### 4.3 `/readyz` could not answer during a Postgres outage (fixed)

The drill recorded `readyz=None` - no response at all inside the client's 15 s - during the exact outage the
endpoint exists to report. Two causes, both in the readiness path:

* The admin-key lookup that authorises the per-dependency detail is itself a Postgres read, and was
  unbounded. It is now bounded by `readiness_probe_timeout_s` and tolerant of its own failure: the response
  drops `checks` rather than dropping the endpoint. This is why `postgres_down` shows `ready=false` with no
  per-dependency detail - that is correct, not missing data.
* The four dependency probes ran **sequentially**, making the endpoint's worst case the *sum* of the per-probe
  budgets (4 x 3 s) while each individual probe was correctly bounded. They are independent and now run under
  `asyncio.gather`, so the worst case is the slowest probe.

Related, found earlier in the same work: `/readyz` had no per-probe bound at all, so a dependency that *hung*
rather than refused hung readiness itself (~30 s). `readiness_probe_timeout_s` (3 s) now bounds each probe and
reports `fail: timeout after 3.0s`. A probe that cannot answer within its budget **is** a failing probe.

### 4.4 The app could not run on Windows at all (fixed)

`uvicorn app.main:app` on Windows runs a `ProactorEventLoop`, which psycopg's async pool refuses. The app
booted anyway and served traffic with every Postgres operation failing and `/readyz` reporting postgres down,
with no startup error. `create_app` now refuses to start on such a loop, naming the fix.

The first attempt at the fix did not work, and the guard is what caught it: uvicorn 0.36 replaced
`Config.setup_event_loop()` with `Config.get_loop_factory()` and builds its loop from an explicit factory that
hard-returns `ProactorEventLoop` on win32, so `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`
is simply **ignored by the server**. `app/main.py` now passes `loop="asyncio:SelectorEventLoop"` (uvicorn
imports a non-preset `loop=` value and uses it as the factory) and exposes a `python -m app.main` entrypoint.
The test asserts the loop uvicorn would *actually construct*, not that a policy was set - the policy
assertion would have passed while the app stayed broken.

## 5. The harness had to be tested before it could be trusted

Four defects in the drill itself were found by `tests/test_chaos_drill.py` (13 tests) and by running it, not
by reading it. They are listed because each one would have produced a *confident wrong answer* about the
application:

| defect | what it would have reported |
|---|---|
| killed services with `taskkill /F /IM postgres.exe` | would also kill an unrelated Postgres on the machine; now resolves the pid **listening on the port** |
| `proc.name()` read *after* the kill raised `NoSuchProcess` | a kill that worked (port closed, service down) filed as "failed, killed nothing" |
| `Popen.terminate()` hit only the `.venv` launcher | orphaned uvicorn kept port 8099 and the pg pool after every run, and the next run blamed itself |
| **`start_service` deadlocked forever on the first restart** | the drill died holding Redis down, with no result file - see below |

The deadlock is the one worth recording in detail, because `timeout=` looked like it covered it. `py-spy`
put the stuck process in `start_service -> subprocess.run -> communicate`, joining the pipe reader threads,
with the event loop stopped and no socket to the API. The restarted msys2 service inherits bash's
stdout/stderr pipe handles even though its own fds are redirected to its own log, and keeps the write end
open for its whole lifetime, so the pipe never reaches EOF. And `subprocess.run(timeout=...)` is **not** a
safety net for that on Windows: it raises `TimeoutExpired` and then, on the `_mswindows` branch, calls
`communicate()` *a second time with no timeout* to drain. Measured against the real Redis start command: a
`timeout=8` call returned after **218 s**, and only because the service was killed by hand. bash's output now
goes to a file - no pipes, nothing to drain, and the inherited file handle is harmless.

Three refusals were added so the drill reports "I could not measure this" instead of blaming the app:

* it refuses to start if its port is already held (two drills were once run at once, raced on port 8099, and
  would have measured a process neither of them started - uvicorn's bind failure went to `DEVNULL`);
* a kill that cannot close the port **aborts** rather than running a `*_down` phase against a healthy
  dependency, where every degradation verdict would fail and read as an application defect;
* a restart that does not reopen the port aborts rather than measuring a recovery that cannot happen.

And the central claim is checked as stated: "the API process was never restarted" is asserted from *the
booted pid (or one of its children) still owning the port*, not from `api.poll() is None`, which is true of
any live process including a replacement. The `.venv/Scripts/python.exe` launcher re-execs the real
interpreter as a child, so the pid that holds the port is never the pid `Popen` returned - anything reasoning
about "the API process" has to mean the whole tree.

## 6. What is still not covered

* No failover drill: the CloudNativePG manifest in [16_POSTGRES_HA.md](16_POSTGRES_HA.md) is not exercised
  here, and a primary promotion is a different event from a hard kill.
* No slow-dependency drill. Everything here either works or is dead; a dependency that answers in 10 s is the
  harder case, and the only part of it tested so far is `/readyz` (via the hanging-probe unit tests).
* Single replica, no load balancer, so nothing is said about how a fleet behaves when one replica goes
  unready.
* `breaker_reset_s` (30 s) is not tuned against anything - it is a default, and §3 shows it is what sets the
  user-visible recovery time for a Qdrant outage.
