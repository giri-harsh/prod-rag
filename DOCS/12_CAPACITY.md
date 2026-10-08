# 12 - Capacity and load-test results

**Read this first.** Every number below was produced by a run during development; anything that was not measured is labelled *unverified*.
All runs: ONE laptop (i5-12500H, 12 cores / 16 threads, 16 GB, Windows 11) that also hosted the load generator (Locust), Postgres 16.2
(embedded `pgserver` build), Redis 8.10.2, Qdrant 1.19.2 and **4 uvicorn workers**. LLM, embeddings and the NeMo guard model were **simulated**
(`APP_ENV=loadtest`: LLM 800 +- 300 ms, embed 80 +- 30 ms, guard 250 ms); auth, rate limiting, hybrid Qdrant search (real fastembed BM25 + fake hash
dense vectors), Redis caches, Postgres checkpoints, the NeMo self-check actions, the injection screen and (where stated) the real FlashRank
reranker were real. So: **real datastores, fake upstream models, non-production hardware.** Use the numbers for relative comparisons and as a floor
for the application's own CPU cost, not as an SLA.

Scenario (`loadtest/locustfile.py`): 50 tenants x 66-75 chunks, mix 70% technical / 20% follow-up / 10% blocked, 0.5-2 s think time, 3 minutes per step,
caches cold at the start of each run. Single run per cell (no repeats), so differences of a few percent are noise.

## 1. History of the fixes (reranker disabled in these runs)

| run | change | users | RPS | p50 ms | p95 ms | p99 ms | err % |
|---|---|---|---|---|---|---|---|
| 1 | baseline | 50 / 200 / 500 | 21.6 / 42.9 / 15.4 | 510 / 1,200 / 29,000 | 2,600 / 16,000 / 55,000 | 3,000 / 28,000 / 66,000 | 0 |
| 2 | checkpoint `durability="exit"` | 50 / 200 / 500 | 16.9 / 19.1 / 20.3 | 1,500 / 9,200 / 22,000 | 4,200 / 13,000 / 32,000 | 5,400 / 14,000 / 34,000 | 0 |
| 3 | NeMo self-check actions called directly | 50 / 200 / 500 | 24.3 / **97.1 / 128.6** | 490 / 520 / 2,800 | 2,300 / 2,400 / 4,900 | 2,800 / 2,900 / 5,800 | 0 |

* Bottleneck 1 (in-process profile, 100 requests): ~13 sequential LangGraph checkpoint writes per request through the saver's single lock.
  `durability="exit"` cut DB operations from ~1,280 to ~200 per 100 requests (in-process throughput 20 -> 36 req/s). Raising `LLM_CONCURRENCY` 32 -> 400 and `PG_POOL_MAX` 10 -> 40 changed nothing (43.6 vs 42.9 RPS).
* Bottleneck 2 (cProfile, 120 requests): the NeMo Colang flow engine (~15.8k `compile` calls, 6.5 s). Calling NeMo's own `self_check_input/output` actions directly cost 1.0 ms instead of 19.6 ms per rail call;
  in-process throughput at 300 concurrent requests went 11.4 -> 47.5 req/s. `GUARD_ENGINE=runtime` restores the full engine.
* Run 2 was slower than run 1 although it contains a fix: run-to-run variance on this machine is large.

## 2. With the REAL reranker (FlashRank ONNX, bounded pool of 2 threads, 1 ONNX thread each, passages cut to 512 tokens)

| FUSED_K | users | RPS | p50 ms | p95 ms | p99 ms | err % | semantic-cache hit % |
|---|---|---|---|---|---|---|---|
| 20 | 50  | 18.9 | 370 | 4,400 | 6,300 | 0 | 54.8 |
| 20 | 200 | 36.8 | 500 | 21,000 | 22,000 | 0 | 65.9 |
| 20 | 500 | 52.8 | 5,700 | 25,000 | 27,000 | 0 | 72.7 |
| **10** | 50  | 21.7 | 360 | 3,000 | 3,500 | 0 | 54.1 |
| **10** | 200 | 60.2 | 1,400 | 5,700 | 7,600 | 0 | 66.5 |
| **10** | 500 | 63.3 | 6,600 | 10,000 | 12,000 | 0 | 71.1 |

Compared with run 3 (no reranker, 128.6 RPS at 500 users) the real reranker costs ~50-60% of peak throughput even at K=10, because every semantic-cache miss reranks `FUSED_K` candidates
(measured earlier with ONNX using all cores: ~210 ms per rerank of 20 chunks of ~3,600 characters).

### Choosing FUSED_K
Rule: use 10 if its p95 improves by >= 20% AND hit@5 drops by <= 2 points; otherwise keep 20.

* p95 improvement of K=10 over K=20: -32% (50 users), -73% (200), -60% (500)  -> condition 1 met at every step.
* hit@5 on the labeled set (`loadtest/labeled_set.json`: 30 questions about `DATA/true_data`, 6 source files; `python loadtest/eval_hit_at_k.py`, result in `loadtest/hit_at_k_result.json`):

| FUSED_K | hit@5 (expected source in top 5) | phrase@5 (answer-bearing phrase in top 5) |
|---|---|---|
| 20 | 100.0% | 96.7% |
| 10 | 100.0% | 100.0% |

  Drop = 0.0 points -> condition 2 met. **Decision: `FUSED_K=10` is now the default.**
* Limits of this measurement: the **dense side is fake** (hash bag-of-words, not a semantic model), only 6 documents / 66 chunks, and hit@5 saturates; it shows K=10 does not hurt in this setup, not that recall is unchanged on a large real corpus.
  Re-run the script on your own labeled set before relying on it (*unverified* for real embeddings and large corpora).

## 3. Per-pod capacity and the pod formula
Measured peak with the real reranker at K=10: **63 RPS for 4 workers (~16 RPS/worker)** on this shared laptop. Planning derate (shared hardware, real network I/O, noisy neighbours): use 70% -> **~11 RPS/worker, ~44 RPS per 4-worker pod**.
*Unverified on production hardware; re-measure before committing.*

```
pods = ceil( peak_rps / (workers_per_pod * rps_per_worker * 0.7) )     # 4 * 16 * 0.7 = 44.8 RPS per pod
postgres_connections = pods * workers * PG_POOL_MAX                     # put pgbouncer in front (compose + k8s base include it)
upstream_llm_calls_per_s = peak_rps * (1 + miss_rate * (1 + output_guard + rewrites))   # 1.9 per request at a 70% semantic-cache hit rate
```

"Millions of users" - assumptions stated explicitly (not measured): 1 M (or 10 M) daily active users, 5 queries each, peak = 5x average.

| | 1 M DAU | 10 M DAU |
|---|---|---|
| average RPS | 58 | 580 |
| peak RPS | ~290 | ~2,900 |
| API pods at ~44 RPS/pod | ~7 | ~66 |
| upstream LLM calls/s (x1.9) | ~550 (~33k/min) | ~5,500 (~330k/min) |

The LLM provider quota, not the API tier, is the ceiling at these figures. The call-rate numbers are derived from the assumptions above and this load test's 70% hit rate; **Groq's actual limits for
`llama-3.3-70b-versatile` / `llama-3.1-8b-instant` are not asserted here (unverified) - read them in your Groq console** and compare. Levers: higher semantic-cache hit rate, a provisioned-throughput agreement or several
organisations behind Portkey load balancing, a second provider in the fallback chain, a smaller guard model, per-tenant token budgets.

## 4. Reproduce
```bash
# datastores running, then:   (env: APP_ENV=loadtest DATABASE_URL REDIS_URL QDRANT_URL QDRANT_COLLECTION=loadtest_rag LOADTEST_REAL_RERANKER=true FUSED_K=10 FLASHRANK_CACHE=...)
uvicorn app.main:app --workers 4
LOAD_KEYS=keys.json locust -f loadtest/locustfile.py --headless -u 200 -r 50 -t 3m --host http://127.0.0.1:8080 --csv out/step200
PYTHONPATH=. python loadtest/eval_hit_at_k.py --ks 20 10      # hit@5 on the labeled set
```
