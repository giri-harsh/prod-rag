# Enterprise Multi-Tenant RAG API

FastAPI + LangGraph RAG service: API-key auth with tenant isolation, Postgres-backed conversation memory, NeMo input/output
guardrails (fail-closed), indirect-injection screening, **hybrid search** (dense Gemini + sparse BM25, server-side RRF, FlashRank
rerank), a **self-correcting cyclic graph**, Redis rate limits and caches, a semantic answer cache, arq ingestion workers,
Prometheus metrics (internal port) and PII-redacted Logfire/LangSmith telemetry.

## What it is (and is not)
- The agent is a **LangGraph graph with a real cycle**: `retrieve -> grade -> rewrite_query -> retrieve` (max 2 retrievals,
  graded by the reranker score; the LLM is used only for the rewrite). Exhaustion returns a fixed "no relevant context" answer.
- Stateless API workers (gunicorn + uvicorn). State lives in Postgres (keys, tenants, memory), Redis (limits, caches, jobs) and Qdrant (vectors, semantic cache).
- **No response streaming** (decision): the fail-closed output guard needs the complete draft before anything is sent.
- Verified by an offline test-suite and, locally, against real Postgres 16, Redis 8.10 and Qdrant 1.19.2 servers (see `DOCS/12_CAPACITY.md`
  for load numbers). Live Groq/Portkey/Gemini behaviour is covered only by `tests/live/` (skipped without keys).

## Request flow
```mermaid
graph TD
  C[Client + X-API-Key] --> A[Auth -> tenant/user]
  A --> R[Redis rate limit -> 429 / in-process fallback if Redis is down]
  R --> G[gate_and_plan: input rail || planner JSON, tenant domain]
  G -->|blocked| X[refusal]
  G -->|conversational| S[respond]
  G -->|standalone search| K{semantic cache}
  K -->|hit| Z[answer]
  K -->|miss| T
  G -->|follow-up search| T[retrieve: hybrid dense+BM25 -> RRF -> rerank -> injection screen]
  T -->|no hits| N[fixed no-context answer]
  T --> D{grade: rerank score}
  D -->|good| S
  D -->|poor, iterations left| W[rewrite_query]
  W --> T
  D -->|poor, exhausted| N
  S --> O[output guard: regex + NeMo rail] --> P[(Postgres checkpoint tenant:user:session)]
```

## API
| Endpoint | Auth | Notes |
|---|---|---|
| `POST /query` `{q, session_id, [tenant_id], [include_contexts]}` | `X-API-Key` | `thread_id` is server-derived (client `thread_id` -> 400); wrong `tenant_id` -> 403; 401 bad key; 429 limited; 503 (+`Retry-After`) when Qdrant/LLM/guardrails are down; `include_contexts` needs the `eval` key scope |
| `POST /ingest` `{tenant_id, path, source_type?, rebuild?}` | admin key | Enqueues an arq job (path inside `INGEST_ROOT`) |
| `GET /graph` | admin key | Mermaid text (shows the cycle) |
| `GET /healthz` | none | `{"status":"ok"}` only |
| `GET /readyz` | none / admin | `{"ready": bool}`; per-dependency detail only with an admin key |
| metrics | internal port `9100` | `/metrics` is **not** on the API port. Multiprocess-safe (`PROMETHEUS_MULTIPROC_DIR`) |

## Run it
```bash
cp .env.example .env              # fill GROQ_API_KEY, PORTKEY_API_KEY, GEMINI_API_KEY
docker compose up -d --build      # postgres, pgbouncer, redis, qdrant, migrate (one-shot), api, worker
docker compose run --rm api python -m app.auth.cli create --tenant acme --user alice          # prints the key ONCE
docker compose run --rm api python -m app.auth.cli create --tenant acme --user root --admin
docker compose run --rm api python -m app.auth.cli create --tenant acme --user ci --scope eval
docker compose run --rm api python -m app.auth.cli tenant-set --tenant acme --domain "Kubernetes and networking"
docker compose run --rm api python -m app.ingestion.cli /data/true_data --tenant acme --source-type true
docker compose run --rm api python -m app.ingestion.cli /data/true_data --tenant acme --rebuild   # blue/green: re-embeds + atomic alias swap
curl -H "X-API-Key: rag_..." -H 'content-type: application/json' -d '{"q":"how does pod autoscaling work?","session_id":"s1"}' localhost:8080/query
```
Local tests (Python 3.11): `uv venv --python 3.11 .venv && uv pip install --python .venv -r requirements-dev.lock && .venv/bin/pytest`
(Windows: `.venv\Scripts\pytest`). Against real services set `DATABASE_INTEGRATION_URL`, `REDIS_INTEGRATION_URL`, `QDRANT_INTEGRATION_URL`.
Kubernetes: `kubectl apply -k deploy/k8s/base` (create `rag-secrets` first, see `deploy/k8s/base/secret.example.yaml`).
Load test: `APP_ENV=loadtest` (fake LLM/embedder with simulated latency, real datastores) + `loadtest/locustfile.py`.

> **Index upgrade:** the collection schema is now hybrid (`hybrid-v1`, named vectors). Existing indexes are refused at startup
> with a clear message; run `python -m app.ingestion.cli <dir> --tenant <t> --rebuild`.

## Layout
```
app/api.py            FastAPI factory (auth, limits, routes, error mapping, request ids)
app/container.py      service wiring (all external clients; tests inject fakes here)
app/agents/graph.py   async LangGraph pipeline with the retrieve/grade/rewrite cycle
app/guardrails/       NeMo rails (fail-closed) + injection screen
app/services/         llm, embedding (+Redis cache), sparse (BM25), vectorstore (hybrid, blue/green), reranker, semcache, planner cache
app/auth/             API keys (sha256, scopes), tenant domains, CLI
app/db/               migrations, thread-TTL cleanup
app/ingestion/        loaders, heading-aware chunker, idempotent blue/green pipeline, CLI
app/observability/    metrics, PII redaction (Logfire scrubbing, logging filter, LangSmith hooks)
app/worker.py         arq worker (ingest jobs + cleanup crons)   app/loadtest.py  load-test fakes
deploy/k8s/base       kustomize: api, worker, pgbouncer, HPA, PDB, migrate job
loadtest/             Locust scenario      tests/  pytest suite (+ tests/live gated on keys)
```

## Documentation index
[01 Overview](DOCS/01_SYSTEM_OVERVIEW.md) · [02 Ingestion](DOCS/02_INGESTION_ENGINE.md) · [03 Nodes](DOCS/03_NODE_INTELLIGENCE.md) ·
[04 Tracing](DOCS/04_TRACING_AND_OBSERVABILITY.md) · [05 Environment](DOCS/05_ENVIRONMENT_VARIABLES.md) · [06 Gotchas](DOCS/06_KNOWN_GOTCHAS.md) ·
[07 FlashRank](DOCS/07_FLASHRANK_RERANKING.md) · [08 Guardrails](DOCS/08_GUARDRAILS.md) · [09 LLM Gateway](DOCS/09_LLM_GATEWAY.md) ·
[10 Evals](DOCS/10_EVALS.md) · [11 Evals pipeline](DOCS/11_EVALS_PIPELINE.md) · [12 Capacity](DOCS/12_CAPACITY.md).
Docs 01, 04, 06, 07, 10, 11 start with a "current state" banner; older text below it describes the prototype.
