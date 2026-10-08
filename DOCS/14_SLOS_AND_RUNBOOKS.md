# 14 - SLOs, alert rules and runbooks

What this covers: the service level objectives, the Prometheus rules that watch them
(`deploy/observability/prometheus/alerts.yml`), the Grafana dashboard
(`deploy/observability/grafana/rag_api_dashboard.json`), and one runbook per alert.

**Honesty note.** The thresholds here are *targets* chosen from the load-test numbers in
[12_CAPACITY.md](12_CAPACITY.md) and from the live run in `live_report.md` - they are **not** baselines measured from
production traffic, because this system has none. Re-tune them after a few weeks of real traffic; the rule unit tests
make that safe to do. What *is* verified: every rule compiles and behaves as described (`promtool test rules`, 48 assertions
across 17 alerts), and every metric and label the rules and the dashboard use is really exported by the app
(`scripts/check_metric_names.py`). Run both with `bash scripts/check_alerts.sh`.

## 1. Scrape contract

| job | target | what it serves |
|---|---|---|
| `rag-api` | `METRICS_PORT` (9100) on every API pod | `http_*` from prometheus-fastapi-instrumentator plus the `rag_*` application metrics |
| `rag-worker` | `WORKER_METRICS_PORT` (9101) on every arq worker | `rag_ingest_queue_depth`, `rag_worker_heartbeat_timestamp_seconds`, and the `rag_*` counters the worker touches |

Neither port is exposed by the Kubernetes Service (`deploy/k8s/base/api.yaml` publishes only `http`). `/metrics` does not
exist on the API port. Example scrape configs: `prometheus.yml` (Kubernetes SD) and `prometheus.local.yml` (docker-compose).

Two traps the rules work around, both real:

* **Status is grouped.** The instrumentator exports `status="2xx"|"4xx"|"5xx"`, so a 429 cannot be separated from any
  other 4xx with `http_requests_total`. Rate limiting is measured with `rag_rate_limited_total{window}` instead.
* **The per-handler latency histogram only has 0.1 / 0.5 / 1 s buckets.** A p95 of 5 s is not representable there, so
  the latency rules use the unlabelled `http_request_duration_highr_seconds` histogram (all handlers; `/healthz` and
  `/readyz` are not instrumented).

Gauge semantics in multiprocess mode (gunicorn) are chosen per metric and matter for the rules:
`rag_circuit_breaker_open` is `livesum` (how many worker processes have the breaker open), while `rag_ready`,
`rag_ready_check`, `rag_ingest_queue_depth` and `rag_worker_heartbeat_timestamp_seconds` are `livemostrecent` (the last
process to set it wins, and a process that never set it contributes nothing).

## 2. SLOs

| id | objective | window | measured as | alerts |
|---|---|---|---|---|
| SLO-A | 99.5 % of requests answered without a 5xx | 30 d rolling | `http_requests_total{status="5xx"}` ratio | fast burn (14.4x) and slow burn (6x) |
| SLO-L1 | 95 % of requests under 5 s | 5 m rolling | `http_request_duration_highr_seconds` p95 | `RagLatencyP95High` (>5 s), `RagLatencyP95Critical` (>10 s) |
| SLO-C1 | guardrails block no more than 5 % of legitimate on-domain questions and let through no more than 5 % of attacks | per release | the live adversarial sets, see [08_GUARDRAILS.md](08_GUARDRAILS.md) | `RagGuardrailBlockSpike` (regression proxy only) |
| SLO-I1 | an ingestion job starts within 15 minutes of being queued | 15 m | `rag_ingest_queue_depth` | `RagIngestQueueBacklog`, `RagIngestWorkerStalled` |

The 0.5 % monthly error budget is ~3.6 h of total unavailability, or 1 in 200 requests. Burn-rate alerting (two windows
per severity) is used instead of a plain threshold so a short blip does not page and a slow leak is not missed.

**Deliberately not an SLO:** answer quality. RAGAS faithfulness and answer relevancy are release gates
([10_EVALS.md](10_EVALS.md)), not alertable signals - there is no ground truth at request time.

## 3. Reading the dashboard

Import `deploy/observability/grafana/rag_api_dashboard.json` and pick a Prometheus datasource (the `ds` variable; the
dashboard hard-codes no datasource uid). Rows, top to bottom: **SLOs** (availability, error budget consumed, p95,
pods ready), **traffic/errors/latency**, **dependencies and protection** (readiness per check, breakers, rate limits,
key failover), **guardrails, cost and caches**, and the **ingestion worker**. Firing alerts appear as annotations.

Conventions: one unit per panel (never two y-axes), series colored by *name* so a filter does not repaint them,
thresholds drawn as dashed lines at the same values the alerts use, and status colors reserved for state
(`rag_ready`, readiness timeline) rather than for series identity.

## 4. Runbooks

Each alert's `runbook_url` points at the matching section below. Commands assume `kubectl -n rag` and the API image's
own tooling; on a single host substitute `docker compose` and `scripts/`.

First three things, for any alert: `kubectl get pods -n rag`, then `kubectl exec deploy/rag-api -- curl -s
localhost:8080/readyz -H "X-API-Key: $ADMIN_KEY"` (an admin key adds the per-dependency breakdown), then the dashboard
row for the component on the label.

### RagHighErrorRateFastBurn

**Fires:** more than 7.2 % of requests answered 5xx over *both* 5 m and 1 h (14.4x the budget: ~2 % of the monthly
budget per hour). **Severity: critical - page.**
**Impact:** roughly one request in fourteen fails. At this rate the whole month's budget is gone in ~2 days.
**Check:** which error class, on the "Failures surfaced to clients" panel -
`sum by (upstream) (rate(rag_upstream_errors_total{job="rag-api"}[5m]))`. `UpstreamUnavailable`/`CircuitOpen` means the
model provider, `RetrievalUnavailable` means Qdrant, `GuardrailUnavailable` means the guard model or the rails.
Anything that is *not* in that counter is an unhandled 500: `kubectl logs deploy/rag-api | grep "unhandled exception"`.
**Act:** follow the runbook for the dominant class below. If it is unhandled 500s and a deploy went out in the last
hour, roll back first (`kubectl rollout undo deploy/rag-api`) and investigate from the logs afterwards.

### RagHighErrorRateSlowBurn

**Fires:** more than 3 % of requests answered 5xx over both 30 m and 6 h (6x the budget). **Severity: warning - ticket.**
**Impact:** the budget is leaking; left alone the SLO is missed this month.
**Check:** the same breakdown as the fast burn, over a 6 h window. A steady 3-5 % is usually *one* dependency flapping,
or one tenant hitting a path that always fails (check the per-handler series).
**Act:** fix within the working day. If the cause is a provider's rate limit, see `RagUpstreamFailureRate`.

### RagReadinessFailing

**Fires:** `rag_ready == 0` on a pod for 5 minutes. **Severity: warning.**
**Impact:** that pod is out of the Service's endpoint list; capacity is reduced but users still get answers from the
other pods.
**Check:** `rag_ready_check{check=...} == 0` names the dependency - `postgres`, `redis`, `qdrant`, `embedding_config`
or `guardrails`. Then `kubectl exec <pod> -- curl -s localhost:8080/readyz -H "X-API-Key: $ADMIN_KEY"`.
**Act:**
* `postgres` - check pgbouncer and the cluster (see [16_POSTGRES_HA.md](16_POSTGRES_HA.md)); `PG_POOL_MAX` exhaustion
  looks like this too.
* `redis` - see `RagLimiterFallbackActive`; the API keeps serving, with approximate rate limits.
* `qdrant` - retrieval is down for that pod; check the Qdrant pods and `QDRANT_TIMEOUT_S`.
* `embedding_config` - **do not restart.** The index was built with a different embedding model or dimension than
  `EMBEDDING_MODEL`/`EMBEDDING_DIM`. Either put the old values back or re-ingest; a mismatch silently destroys
  retrieval quality, which is why startup refuses to serve.
* `guardrails` - the NeMo engine failed to initialise (bad `GUARD_MODEL`, missing key). Every request fails closed
  with 503 by design. Fix the config and restart the pod.

### RagNotReadyFleet

**Fires:** no API pod is ready for 2 minutes. **Severity: critical - page.**
**Impact:** total outage; the Service has no endpoints.
**Check:** one shared dependency is almost always the cause - look at `rag_ready_check` across pods, and at whether a
rollout is in progress (`kubectl rollout status deploy/rag-api`).
**Act:** if a deploy is in flight, `kubectl rollout undo deploy/rag-api`. Otherwise treat it as an outage of whichever
dependency `rag_ready_check` names and work the matching bullet above. The readiness probe is intentionally strict -
do not "fix" the outage by removing it.

### RagApiTargetDown

**Fires:** Prometheus cannot scrape `job="rag-api"` for 3 minutes, or the job is absent entirely. **Severity: critical.**
**Impact:** every other alert for the API is blind. This is a monitoring outage, not necessarily a service outage -
check `/healthz` through the Service before assuming users are affected.
**Check:** `kubectl get endpoints rag-api`, then from inside the cluster
`curl -s <pod-ip>:9100/metrics | head`. Confirm `METRICS_PORT` matches the scrape config and that the gunicorn master
started the metrics server (`kubectl logs deploy/rag-api | grep -i metrics`).
**Act:** if the pods are healthy, fix the scrape config or the ServiceMonitor. If `PROMETHEUS_MULTIPROC_DIR` is set but
unwritable, the master exits - the logs say so.

### RagLatencyP95High

**Fires:** p95 above 5 s for 10 minutes with real traffic. **Severity: warning.**
**Impact:** slow answers; clients with a 30 s timeout are still fine.
**Check:** the "Request latency quantiles" panel (is p50 up too, or only the tail?) and `live_report.md`'s stage table
for what each stage normally costs. The usual order of suspects: the model provider (look at
`rate(rag_llm_tokens_total[5m])` and key failover), then the retrieval loop
(`rag_degraded_total`/iterations - a query that keeps grading poorly costs 2-3 retrievals plus a rewrite), then the
reranker on CPU-starved pods, then Postgres checkpoint writes.
**Act:** short term, scale out (the HPA in `deploy/k8s/base/hpa.yaml` reacts to CPU, not to latency) or reduce
`FUSED_K`/disable the reranker for the affected tenant. Medium term see [12_CAPACITY.md](12_CAPACITY.md) - the measured
bottlenecks were checkpoint writes and the NeMo flow engine, both already mitigated.

### RagLatencyP95Critical

**Fires:** p95 above 10 s for 5 minutes. **Severity: critical - page.**
**Impact:** clients begin timing out; with `LLM_TIMEOUT_S=30` a request that waits on two upstream retries can exceed
any sane client budget.
**Check:** as above, plus `rag_circuit_breaker_open` - a half-open breaker that keeps probing a dead upstream adds a
full timeout to every probe.
**Act:** if one upstream is the cause, let the breaker open (or lower `BREAKER_FAILURES`) so requests fail fast with
503 + `Retry-After` instead of hanging; that trades the latency SLO for the availability SLO deliberately. Then scale
out and work the upstream.

### RagRateLimited429High

**Fires:** more than 10 % of requests rejected by the per-key minute/day limiter for 10 minutes. **Severity: warning.**
**Impact:** those callers get 429 with `Retry-After`. This is the limiter working, not a failure - but at 10 % it is
either a misconfigured client or a limit that no longer matches the plan.
**Check:** `sum by (window) (rate(rag_rate_limited_total[10m]))` - `minute` means bursts, `day` means a quota is
exhausted. Identify the key with `python -m app.auth.cli list --tenant <t>`.
**Act:** if the traffic is legitimate, issue a key with the right limits and retire the old one - limits are stored per
key at creation, there is no update command:
`python -m app.auth.cli create --tenant <t> --user <u> --per-min N --per-day M` then
`python -m app.auth.cli revoke --id <old-id>`. Otherwise tell the client to back off. Do not raise
`DEFAULT_RATE_PER_MIN` globally to silence one key.

### RagAuthFailure429

**Fires:** more than 0.2 auth-failure rejections per second for 5 minutes (the per-IP penalty box,
`AUTH_FAIL_PER_MIN`, default 20/min). **Severity: warning.**
**Impact:** clients from that IP are being throttled before authentication.
**Known limitation:** `rag_rate_limited_total{window="auth_fail"}` is incremented only when the penalty box
*rejects* a request, so the real number of failed authentications is higher than this series shows. Treat any
sustained non-zero rate as real.
**Check:** `kubectl logs deploy/rag-api | grep -i "auth" | tail -50` for the client IPs. If every failure appears to
come from one IP that is actually your ingress, `TRUSTED_PROXIES` is wrong and the penalty box is punishing all
tenants together.
**Act:** fix `TRUSTED_PROXIES` first if that is the case. Otherwise: credential stuffing (block at the edge) or a
revoked key still in use (find the tenant, tell them to rotate - `python -m app.auth.cli revoke --key-id <id>` is
already in effect).

### RagLimiterFallbackActive

**Fires:** the in-process limiter rejected a request, or the `redis` readiness check has been failing, for 2 minutes.
**Severity: warning.**
**Impact:** Redis is unavailable. The API keeps serving, but: per-key limits become approximate (each worker enforces
`limit / WEB_CONCURRENCY` on its own), the planner and semantic caches miss, and conversation locking falls back to
"proceed unlocked" - two concurrent requests in one session can overwrite each other's history.
**Check:** `kubectl get pods -l app=redis`, then `kubectl exec deploy/rag-api -- redis-cli -u $REDIS_URL ping`.
**Act:** restore Redis. Nothing in the API needs restarting afterwards - the limiter and the caches reconnect on the
next request (verified in the chaos drill, [15_CHAOS_DRILL.md](15_CHAOS_DRILL.md)). While it is down, expect higher
token spend from cache misses.

### RagCircuitBreakerOpen

**Fires:** `rag_circuit_breaker_open` is above 0 for one upstream for 2 minutes. **Severity: warning.**
**Impact:** on those worker processes, calls to that upstream fail fast after `BREAKER_FAILURES` consecutive failures;
affected requests get 503 + `Retry-After` instead of hanging. Other processes may still be serving.
**Check:** the `upstream` label is `llm`, `embeddings`, `qdrant` or `guardrails`. Cross-check with
`increase(rag_circuit_breaker_trips_total[15m])`: a single trip that stays open is an outage, repeated trips are
flapping (usually a provider rate limit, since 429 counts as a failure).
**Act:** work the upstream. The breaker recovers on its own - after `BREAKER_RESET_S` it half-opens and lets a single
probe through; one success closes it. Do not restart pods to "reset" it.

### RagCircuitBreakerOpenFleet

**Fires:** at least half the API pods have that upstream's breaker open, for 2 minutes. **Severity: critical - page.**
**Impact:** the upstream is effectively down for users, not just for one pod.
**Check:** for `llm`/`guardrails`, the provider's status page and `rag_llm_key_failover_total` (if the primary Groq key
is failing, failover should already be carrying the traffic - a rising counter with an open breaker means *both* keys
are failing). For `qdrant`/`embeddings`, the dependency itself.
**Act:** `llm` - verify both keys with `python scripts/check_keys.py`, and whether the provider is rate-limiting the
whole account (per-model limits are separate, so `LLM_MODEL_FAILOVER=true` moving traffic to `LLM_FALLBACK_MODEL` can
buy room). `qdrant` - retrieval is down; the API correctly returns 503 rather than answering without context.

### RagUpstreamFailureRate

**Fires:** one `AppError` class accounts for more than 5 % of `/query` requests over 5 minutes. **Severity: warning.**
**Impact:** depends on the class; all of them are 503 to the caller. The 409s (`SessionBusy`, `RebuildInProgress`) are
excluded from this rule on purpose - they are caller and ops conditions, not outages.
**Check:** the `upstream` label *is* the exception class name. Then the logs:
`kubectl logs deploy/rag-api | grep "request failed" | tail -20` prints the real reason behind the generic public
message.
**Act:** `GuardrailUnavailable` - the guard model is failing; the system is failing closed, which is intended, so fix
the model/key rather than loosening the rails. `UpstreamUnavailable` - the chat model; check keys, quotas and
`LLM_TIMEOUT_S`. `RetrievalUnavailable` - Qdrant. `CircuitOpen` - see the breaker runbooks.

### RagPipelineDegraded

**Fires:** an advisory step was skipped on more than 5 % of `/query` requests for 15 minutes. **Severity: warning.**
**Impact:** requests still answer 200, so this is invisible in the 5xx SLO - but the answers are worse. `step="planner"`
means the search ran on the user's raw question instead of a refined query (and the conversational/search routing was
skipped). `step="rewrite"` means a poorly-graded retrieval could not be retried and the request returned the
"I couldn't find relevant information" answer.
**Check:** both steps run on `LLM_FALLBACK_MODEL` (the cheap model) with small token caps
(`FEATURE_MAX_TOKENS`: planner 512, rewrite 256). `kubectl logs deploy/rag-api | grep -E "planner unavailable|query
rewrite unavailable"` prints the provider's own reason. Two seen in practice: `finish_reason=length` (the reasoning
budget ate the whole cap - raise the cap or lower `LLM_REASONING_EFFORT`) and `finish_reason=stop` with no content
(the model chose to answer nothing for that input; retrying is pointless at temperature 0).
**Act:** if it is `length`, raise the feature cap. If it is `stop` on a narrow set of inputs, that is the degradation
working as designed - no action beyond watching the rate. If the rate is high across all inputs, the cheap model or
its key is unhealthy: check `rag_llm_key_failover_total` and consider pointing `LLM_FALLBACK_MODEL` at another model.

### RagGuardrailBlockSpike

**Fires:** the 10-minute block rate is at least 3x the trailing 6-hour average and above 0.1 blocks/s.
**Severity: warning.**
**Impact:** either an attack wave (working as intended) or a regression that is refusing legitimate questions.
**Check:** break down by `stage` and `rail` on the guardrails panel. `stage="input"` with
`rail="self check input"` rising alone is usually an attack wave; `stage="output"` with a deterministic rail name
(`secret_api_key`, `system_prompt_leak`, `exfil_markdown_image`, `private_key`) means the responder tried to emit
something it should not - treat that as a serious finding and read the logs. A spike right after a deploy that touched
the rail prompts, the responder prompt or `GUARD_MODEL` is a regression.
**Act:** regression - roll back the change and re-run the calibration set ([08_GUARDRAILS.md](08_GUARDRAILS.md));
false-block and false-pass rates are measured there, not here. Attack wave - no action beyond rate-limiting the
source; the rails are doing their job.

### RagIngestQueueBacklog

**Fires:** more than 20 jobs waiting for 15 minutes. **Severity: warning.**
**Impact:** newly uploaded documents are not searchable yet. Queries are unaffected.
**Check:** `rag_ingest_queue_depth` is the `ZCARD` of the arq queue and **includes deferred jobs**, so a backlog of
retries looks the same as a backlog of new work. Confirm the workers are alive
(`RagIngestWorkerStalled` would also be firing if not) and check whether a rebuild holds the lock:
`kubectl logs deploy/rag-worker | grep -i rebuild`.
**Act:** scale `deploy/rag-worker`. If one job keeps failing and re-queueing, find it in the worker logs and fix the
document (a single corrupt PDF can hold a slot). A full rebuild is expected to produce a temporary backlog - check
before scaling.

### RagIngestWorkerStalled

**Fires:** no worker has refreshed its heartbeat for over 3 minutes, or no worker target is scraped at all.
**Severity: warning.**
**Impact:** queued ingestion jobs are not being picked up. Queries are unaffected.
**Check:** the heartbeat is stamped every 15 s by each replica *after* a successful Redis `ZCARD`, so it stops for
three reasons: the worker is dead, it is blocked on a long job, or it cannot reach Redis.
`kubectl get pods -l app=rag-worker` and `kubectl logs deploy/rag-worker --tail=50` tell those apart; a Redis problem
also shows up in `RagLimiterFallbackActive`.
**Act:** restart the worker deployment if it is wedged (`kubectl rollout restart deploy/rag-worker`); ingestion jobs
are idempotent per document. If `absent()` is what fired and the pods are healthy, the `rag-worker` scrape job or
`WORKER_METRICS_PORT` is misconfigured - note that `WORKER_METRICS_PORT=0` disables the exporter entirely.

## 5. Validating a change to the rules

```bash
bash scripts/check_alerts.sh          # promtool check config/rules + test rules, then check_metric_names.py
```

`promtool` is optional (install it from a Prometheus release and put it on `PATH`); without it the script degrades to a
YAML + schema check that still verifies every rule has `expr`, a `severity` and a `runbook_url`.

When adding an alert: add it to `alerts.yml`, add at least one firing and one non-firing case to `alerts_test.yml`
(the annotation text is compared exactly, which catches broken templating), add a section here with the anchor the
`runbook_url` points at, and - if it needs a new metric - export it from `app/observability/metrics.py` so
`check_metric_names.py` can see it.
