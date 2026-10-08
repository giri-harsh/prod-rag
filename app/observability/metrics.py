"""Prometheus metrics. Works in multiprocess mode when PROMETHEUS_MULTIPROC_DIR is set (gunicorn).

Gauge modes matter in multiprocess mode (each gunicorn worker writes its own file; the metrics server merges them):
  * livesum        - values add up across live workers (breakers: "how many workers have it open")
  * livemostrecent - the most recently SET value wins (readiness: whichever worker answered the last probe);
                     a worker that never set the gauge contributes nothing, so a fresh worker cannot mask a failure
"live*" modes drop the series of dead workers (gunicorn.conf.py calls mark_process_dead in child_exit).
Worker-process gauges (queue depth, heartbeat) also use livemostrecent: the API processes import this module too, and a
gauge that was never SET is then not exported at all (livemax/livesum would export a bogus 0 from every API worker).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge

LLM_TOKENS = Counter("rag_llm_tokens_total", "LLM tokens by feature and kind", ["feature", "kind"])
GUARDRAIL_BLOCKS = Counter("rag_guardrail_blocks_total", "Guardrail blocks", ["stage", "rail"])
INJECTION_CHUNKS = Counter("rag_injection_chunks_total", "Retrieved chunks flagged by injection screen", ["action"])
RATE_LIMITED = Counter("rag_rate_limited_total", "Requests rejected by rate limiter", ["window"])
for _w in ("minute", "day", "fallback", "auth_fail"):  # export explicit zeros so rate() sees the very first rejection
    RATE_LIMITED.labels(_w)
UPSTREAM_ERRORS = Counter("rag_upstream_errors_total", "Upstream failures surfaced to client", ["upstream"])

LLM_FAILOVER = Counter("rag_llm_key_failover_total", "Direct-Groq calls that failed over to the fallback key", ["reason"])

# Advisory pipeline steps (planner, query rewrite) that failed and were skipped instead of failing the request. The
# request still answered, so it does NOT show up in rag_upstream_errors_total - this counter is the only signal.
DEGRADED = Counter("rag_degraded_total", "Requests answered with an advisory pipeline step skipped after it failed", ["step"])
for _s in ("planner", "rewrite"):
    DEGRADED.labels(_s)

# --- resilience (app/resilience.py) ---
CIRCUIT_OPEN = Gauge(
    "rag_circuit_breaker_open",
    "1 while a worker's circuit breaker for the upstream is open or half-open (tripped and not yet recovered); summed over live workers",
    ["upstream"], multiprocess_mode="livesum",
)
CIRCUIT_TRIPS = Counter("rag_circuit_breaker_trips_total", "Times a circuit breaker tripped open (closed->open or failed half-open probe)", ["upstream"])

# --- readiness (GET /readyz in app/api.py) ---
READY = Gauge("rag_ready", "1 if the most recent /readyz probe passed, 0 if it failed", multiprocess_mode="livemostrecent")
READY_CHECK = Gauge(
    "rag_ready_check", "Per-dependency result of the most recent /readyz probe (1 ok, 0 failing)", ["check"], multiprocess_mode="livemostrecent"
)

# --- ingestion worker (app/worker.py, served on WORKER_METRICS_PORT) ---
INGEST_QUEUE_DEPTH = Gauge(
    "rag_ingest_queue_depth", "Jobs waiting in the arq queue (ZCARD, includes deferred jobs)", multiprocess_mode="livemostrecent"
)
WORKER_HEARTBEAT = Gauge(
    "rag_worker_heartbeat_timestamp_seconds", "Unix time of the worker's last successful queue-depth refresh", multiprocess_mode="livemostrecent"
)
