# System overview

A multi-tenant RAG API. One FastAPI service (gunicorn/uvicorn workers) in front of Postgres, Redis and Qdrant, calling Groq (via Portkey) for generation and Gemini for embeddings.
What was *measured* is stated in [12 Capacity](12_CAPACITY.md); what was never run (Docker image, GitHub CI, live Groq/Portkey/Gemini) is listed in the README's "Verification status" and covered by `scripts/verify_live` and `scripts/verify_docker`.

## What a request does
```mermaid
graph TD
  C[Client + X-API-Key] --> A[Auth -> tenant/user]
  A --> R[Redis rate limit]
  R --> G[gate_and_plan: input rail || planner]
  G -->|blocked| X[refusal]
  G -->|conversational| S[respond]
  G -->|first-turn search| K{semantic cache}
  K -->|hit| Z[answer]
  K -->|miss| T
  G -->|follow-up search| T[retrieve: dense+BM25 -> RRF -> rerank -> injection screen]
  T -->|no hits| N[fixed no-context answer]
  T --> D{grade: rerank score}
  D -->|good| S
  D -->|poor, iterations left| W[rewrite_query] --> T
  D -->|poor, exhausted| N
  S --> O[output guard] --> P[(Postgres checkpoint)]
```

## Design decisions worth knowing
* **Tenant isolation is structural**: the thread id is derived server-side (`tenant:user:session`), every Qdrant query carries a `tenant_id` filter, caches are tenant-scoped or keyed by tenant.
* **Fail closed**: if the NeMo rails are not initialised or error, `/query` returns 503; the answer is only returned if the output guard ran.
* **No response streaming**: the output guard needs the complete draft before anything is sent to the client.
* **Retrieved text is untrusted data**: regex screen + explicit `<retrieved_context>` delimiters + a system rule.
* **Stateless workers**: all state is in Postgres (keys, tenants, conversation checkpoints), Redis (limits, caches, jobs, locks) and Qdrant (vectors, semantic cache).

## Code map
`app/api.py` (routes, auth, errors) - `app/container.py` (wiring; tests inject fakes here) - `app/agents/graph.py` (the pipeline) - `app/guardrails/` - `app/services/` (llm, embedding, vectorstore, reranker, sparse, caches, rebuild) -
`app/auth/` - `app/db/` - `app/ingestion/` - `app/observability/` - `app/worker.py` - `deploy/k8s/base` - `loadtest/` - `scripts/` - `tests/`.

Next: [02 Ingestion](02_INGESTION_ENGINE.md) - [03 Nodes](03_NODE_INTELLIGENCE.md) - [04 Observability](04_TRACING_AND_OBSERVABILITY.md) - [05 Environment](05_ENVIRONMENT_VARIABLES.md).
