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
`rag_limiter_fallback_total`, `rag_embed_cache_total{result}`, `rag_semcache_total{result}`, `rag_upstream_errors_total{upstream}`.

## Health
`GET /healthz` -> `{"status":"ok"}` (liveness, no dependency checks). `GET /readyz` -> `{"ready": bool}`; an admin key additionally sees per-dependency results (Postgres, Redis, Qdrant, embedding-config match, guardrails).

## Not provided
Alert rules/dashboards, log shipping, and distributed tracing across the Streamlit UI are not part of this repository.
