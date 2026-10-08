"""Prometheus metrics. Works in multiprocess mode when PROMETHEUS_MULTIPROC_DIR is set (gunicorn)."""
from __future__ import annotations

from prometheus_client import Counter

LLM_TOKENS = Counter("rag_llm_tokens_total", "LLM tokens by feature and kind", ["feature", "kind"])
GUARDRAIL_BLOCKS = Counter("rag_guardrail_blocks_total", "Guardrail blocks", ["stage", "rail"])
INJECTION_CHUNKS = Counter("rag_injection_chunks_total", "Retrieved chunks flagged by injection screen", ["action"])
RATE_LIMITED = Counter("rag_rate_limited_total", "Requests rejected by rate limiter", ["window"])
UPSTREAM_ERRORS = Counter("rag_upstream_errors_total", "Upstream failures surfaced to client", ["upstream"])

LLM_FAILOVER = Counter("rag_llm_key_failover_total", "Direct-Groq calls that failed over to the fallback key", ["reason"])
