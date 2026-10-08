# Tracing, metrics and logs

## Logfire spans
`create_app()` calls `logfire.configure()` (export only if `LOGFIRE_TOKEN` is set) before any service is built, with PII scrubbing enabled. Spans: `<METHOD> <path>` (carries `request_id`), `gate+plan`, `semantic cache lookup`, `retrieval iteration` (one per loop pass, attribute `iteration`),
`rewrite query`, `respond`, `output guard`, `ingest`. FastAPI is instrumented with `logfire.instrument_fastapi`. Request ids come from `X-Request-ID` (validated) or are generated, are returned in the response header and set as baggage.

## PII redaction (`app/observability/redact.py`) - telemetry only
Regex redaction of email, phone, Aadhaar (12 digits), PAN, card numbers (Luhn-checked), API keys/bearer tokens and IPv4/IPv6 addresses, applied to (1) Logfire span attributes and messages via a scrubbing callback, (2) the root logging filter, (3) LangSmith `hide_inputs/hide_outputs` when `LANGSMITH_TRACING=true`.
The API answer is never redacted. Over-redaction is expected (e.g. long digit groups look like phone numbers). Tests: `tests/test_caches_security.py`.

## Prometheus
Served on a **separate internal port** (`METRICS_PORT`, default 9100; started by the gunicorn master, multiprocess-safe through `PROMETHEUS_MULTIPROC_DIR`); `/metrics` does not exist on the API port. Do not publish it outside the cluster/host.
HTTP latency/error metrics come from `prometheus-fastapi-instrumentator`. Application counters: `rag_llm_tokens_total{feature,kind}`, `rag_guardrail_blocks_total{stage,rail}`, `rag_injection_chunks_total{action}`, `rag_rate_limited_total{window}` (minute, day, fallback, auth_fail),
`rag_limiter_fallback_total`, `rag_embed_cache_total{result}`, `rag_semcache_total{result}`, `rag_upstream_errors_total{upstream}`, `rag_llm_key_failover_total{reason}`,
`rag_degraded_total{step}` (an advisory step - planner or rewrite - failed and was skipped; the request still answered 200).
Gauges: `rag_circuit_breaker_open{upstream}` (+ `rag_circuit_breaker_trips_total{upstream}`), `rag_ready`, `rag_ready_check{check}`, and - on the ingestion worker's own port
(`WORKER_METRICS_PORT`, default 9101, `0` disables) - `rag_ingest_queue_depth` and `rag_worker_heartbeat_timestamp_seconds`.
Multiprocess gauge modes are chosen per metric (`livesum` for breakers, `livemostrecent` for readiness and the worker gauges); see the module docstring in `app/observability/metrics.py`.

## Health
`GET /healthz` -> `{"status":"ok"}` (liveness, no dependency checks). `GET /readyz` -> `{"ready": bool}`; an admin key additionally sees per-dependency results (Postgres, Redis, Qdrant, embedding-config match, guardrails).

## Alerting and dashboards
`deploy/observability/prometheus/` holds the scrape configs (Kubernetes SD and docker-compose), 17 alert rules and their `promtool` unit tests;
`deploy/observability/grafana/rag_api_dashboard.json` is the dashboard. SLOs and one runbook per alert: [14_SLOS_AND_RUNBOOKS.md](14_SLOS_AND_RUNBOOKS.md).
Validate any change with `bash scripts/check_alerts.sh` (promtool plus `scripts/check_metric_names.py`, which fails if a rule or panel names a metric or label the app does not export).

## Not provided
Log shipping, an Alertmanager routing config, and distributed tracing across the Streamlit UI are not part of this repository.
