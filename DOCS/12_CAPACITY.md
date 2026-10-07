# 12 - Capacity and load-test results

**Read this first.** These numbers are from ONE laptop (i5-12500H, 12 cores/16 threads, 16 GB, Windows 11) that also hosted
the load generator, Postgres 16 (pgserver), Redis 8.10, Qdrant 1.19.2 and 4 uvicorn workers. LLM, embeddings and the guard
model were **simulated** (`APP_ENV=loadtest`: LLM 800 +- 300 ms, embed 80 +- 30 ms, guard 250 ms). Everything else
(auth, rate limiting, hybrid Qdrant search + real BM25, Redis caches, Postgres checkpoints, NeMo rails runtime, injection screen)
was real. Treat them as *relative* indicators and a floor for the application's own CPU cost, not a production SLA.
Label: **real Postgres/Redis/Qdrant, fake upstream models.**

## Scenario
Locust, 50 tenants (75 chunks each), mix 70% technical / 20% follow-up / 10% blocked, 0.5-2 s think time, 3 minutes per step
(`loadtest/locustfile.py`). Same server, 4 workers, default settings. Run 1 = baseline, run 2 = after fix 1 (checkpoint durability), **run 3 = after fix 2 (NeMo self-check actions called directly) = current code**.

| run | users | RPS | p50 ms | p95 ms | p99 ms | err % | semantic-cache hit % |
|---|---|---|---|---|---|---|---|
| 1 | 50  | 21.6 | 510   | 2,600  | 3,000  | 0 | n/a (counter not yet recorded) |
| 1 | 200 | 42.9 | 1,200 | 16,000 | 28,000 | 0 | 65.3 |
| 1 | 500 | 15.4 | 29,000 | 55,000 | 66,000 | 0 | 76.0 |
| 2 | 50  | 16.9 | 1,500 | 4,200  | 5,400  | 0 | 69.7 |
| 2 | 200 | 19.1 | 9,200 | 13,000 | 14,000 | 0 | 71.3 |
| 2 | 500 | 20.3 | 22,000 | 32,000 | 34,000 | 0 | 73.6 |
| **3** | 50 | 24.3 | 490 | 2,300 | 2,800 | 0 | 71.5 |
| **3** | 200 | **97.1** | 520 | 2,400 | 2,900 | 0 | 70.1 |
| **3** | 500 | **128.6** | 2,800 | 4,900 | 5,800 | 0 | 70.9 |

* Zero request failures at 500 users: the system queues and degrades, it does not error (no 5xx, no 429; per-key limits were set very high).
* Limiter fallbacks: 0 (Redis stayed up; `rag_limiter_fallback_total` is exercised by tests only).
* 50 users is think-time bound (about 24 RPS is simply what 50 users generate), so it measures latency, not capacity.
* Run-to-run variance on this box is large (run 2 was slower than run 1 at 50 users although it contains fix 1); runs 1-2 saturated at roughly 20-45 RPS, run 3 at ~130 RPS, a ~3-6x gain at 200-500 users with p95 at 500 users down from 32-55 s to 4.9 s.

## First bottleneck and what was changed
1. **LangGraph checkpoint write amplification.** An in-process profile (100 concurrent requests, real Postgres) showed ~13
   sequential checkpoint operations per request (a `put`/`put_writes` after every node), all serialised through the
   saver's single connection lock: `ckpt.put_writes` mean 1.8 s, `ckpt.get` 0.6 s. **Fix (1 line):**
   `graph.ainvoke(..., durability="exit")` writes one checkpoint per request: DB operations for 100 requests fell from ~1,280 to ~200 and
   in-process throughput rose from 20 to ~36 req/s. Raising `LLM_CONCURRENCY` (32 -> 400) and `PG_POOL_MAX` (10 -> 40) had **no** effect (43.6 vs 42.9 RPS), so
   the semaphore and DB pool were ruled out.
2. **CPU in the NeMo Guardrails Colang-1.0 flow engine.** `cProfile` showed `builtins.compile` (~15.8k calls, 6.5 s) and
   `colang/v1_0/runtime/flows.py::compute_next_state` (4.9 s cumulative) for 120 requests: the rails runtime cost more CPU than everything else in the
   request path (one worker was CPU-bound at ~8-12 req/s). **Fix (~35 lines, `app/guardrails/rails.py`):** call NeMo's own `self_check_input` /
   `self_check_output` actions directly (same prompt, same Yes/No parsing, same task manager, structured `RailOutcome`) instead of going through
   `generate_async`'s flow engine: 19.6 ms -> 1.0 ms CPU per rail call. In-process throughput at 300 concurrent requests went from 11.4 to 47.5 req/s,
   and the HTTP runs above followed. `GUARD_ENGINE=runtime` restores the full Colang runtime; `test_guard_engines_agree_on_structured_results` checks both modes give identical block decisions.
   Remaining per-request CPU is spread out (checkpoint serialisation, fastembed BM25, langgraph); the single-connection checkpoint lock is the next candidate (`ckpt.get`/`put` still queue under 300 concurrent requests in one process).

## Per-pod capacity and pod formula
Measured: ~130 RPS for 4 workers on a laptop that is also running the client and every datastore, i.e. ~32 RPS/worker upper bound. Planning number (derated 40% for real network I/O,
larger real payloads and noisy neighbours): **~20 RPS per gunicorn worker** (CPU-bound, independent of LLM latency because LLM waits are async). A pod with 4 workers
(2-4 vCPU request/limit in `deploy/k8s/base/api.yaml`) -> ~80 RPS; keep 30% headroom -> **~56 RPS/pod**. Re-measure on your hardware before committing to it.

```
pods = ceil( peak_rps / (workers_per_pod * rps_per_worker * 0.7) )          # 4 * 20 * 0.7 = 56
postgres_connections = pods * workers * PG_POOL_MAX   -> put pgbouncer in front (compose + k8s base include it)
groq_requests_per_s  = peak_rps * (1 + miss_rate * (1 + output_guard + rewrites))   # = 1.9 per request at a 70% cache hit rate (see below)
```

## "Millions of users" - stated assumptions
| assumption | value |
|---|---|
| daily active users | 1 M (or 10 M) |
| queries per user per day | 5 |
| peak / average ratio | 5x |
| => average RPS | 58 (580) |
| => **peak RPS** | **~290 (~2,900)** |
| => API pods at 56 RPS/pod | **~6 (~52)** |

## The real ceiling is the upstream LLM quota, not the API
Per uncached request: input guard (8B) + planner (70B, usually Redis-cached) + responder (70B) + output guard (8B) = 3-4 Groq calls; a semantic-cache hit costs
1 (input guard only). At 290 peak RPS and a 70% hit rate that is about 290 x (0.7x1 + 0.3x4) = **~550 Groq requests/s = ~33k requests/min**,
and every responder call carries ~2k+ tokens. Groq rate limits are per organisation/model and tier-dependent (requests/min, tokens/min, tokens/day) and
change over time: **read the current limits for `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` in your Groq console** and compare them with the figure above.
Typical standard tiers are orders of magnitude below it, so a million-user deployment needs: an enterprise/provisioned-throughput agreement or several Groq
organisations behind Portkey load-balancing, a second provider in the Portkey fallback chain, a higher semantic-cache hit rate (longer TTL, tenant-shared FAQ corpora),
a smaller/cheaper model for the guards, and per-tenant daily token budgets (the per-key daily request limit already exists).

## Reproduce
```bash
APP_ENV=loadtest DATABASE_URL=... REDIS_URL=... QDRANT_URL=... QDRANT_COLLECTION=loadtest_rag uvicorn app.main:app --workers 4
LOAD_KEYS=keys.json locust -f loadtest/locustfile.py --headless -u 200 -r 50 -t 3m --host http://127.0.0.1:8080 --csv out/step200
```
