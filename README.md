# Enterprise Multi-Tenant RAG API

FastAPI + LangGraph RAG service: API-key auth with tenant isolation, Postgres-backed conversation memory, NeMo input/output
guardrails (fail-closed), indirect-injection screening of retrieved chunks, Qdrant (tenant-filtered, blue/green indexes),
Gemini embeddings, Portkey/Groq LLM gateway, Redis rate limits, arq ingestion workers, Prometheus metrics and Logfire tracing.

## What it is (and is not)
- The agent is a **LangGraph graph with conditional branches** (`gate_and_plan -> retrieve? -> respond -> output_guard`). It is **not cyclic yet**.
- Stateless API workers (gunicorn + uvicorn). All state lives in Postgres (keys, memory), Redis (limits, cache, jobs) and Qdrant (vectors).
- Verified by an offline test-suite (real Postgres + Redis protocol + Qdrant local mode, fake LLM/embeddings). Live-provider
  behaviour (Groq, Portkey, Gemini, Qdrant Cloud) is **not** exercised by tests.

## Request flow
```mermaid
graph TD
  C[Client + X-API-Key] --> A[Auth -> Principal tenant/user]
  A --> R[Redis rate limit 429]
  R --> G[gate_and_plan: input rail || planner (cached)]
  G -->|blocked| X[refusal]
  G -->|conversational| S[respond]
  G -->|search| T[retrieve: embed -> Qdrant(tenant filter) -> FlashRank -> injection screen]
  T -->|no chunks| N[fixed 'no relevant context' answer]
  T --> S
  S --> O[output guard: deterministic + NeMo rail] --> P[(Postgres checkpoint thread tenant:user:session)]
```

## API
| Endpoint | Auth | Notes |
|---|---|---|
| `POST /query` `{q, session_id, [tenant_id]}` | `X-API-Key` | `thread_id` is **server-derived**; sending `thread_id` -> 400; wrong `tenant_id` -> 403; 401 bad/missing key; 429 rate limited; 503 (+`Retry-After`) when Qdrant/LLM/guardrails are unavailable |
| `POST /ingest` `{tenant_id, path, source_type?, rebuild?}` | admin key | Enqueues an arq job (path must be inside `INGEST_ROOT`) |
| `GET /graph` | admin key | Mermaid text of the graph |
| `GET /healthz` / `GET /readyz` | none | Liveness / readiness (Postgres, Redis, Qdrant, embedding-config match, guardrails) |
| `GET /metrics` | none (keep internal) | Prometheus: latency/errors, `rag_llm_tokens_total`, `rag_guardrail_blocks_total`, ... |

## Run it
```bash
cp .env.example .env              # fill GROQ_API_KEY, PORTKEY_API_KEY, GEMINI_API_KEY
docker compose up -d --build      # postgres, redis, qdrant, migrate (one-shot), api, worker
docker compose run --rm api python -m app.auth.cli create --tenant acme --user alice          # prints the key ONCE
docker compose run --rm api python -m app.auth.cli create --tenant acme --user root --admin
docker compose run --rm api python -m app.ingestion.cli /data/true_data --tenant acme --source-type true
docker compose run --rm api python -m app.ingestion.cli /data/true_data --tenant acme --rebuild   # blue/green swap
curl -H "X-API-Key: rag_..." -H 'content-type: application/json' -d '{"q":"how does pod scheduling work?","session_id":"s1"}' localhost:8080/query
```
Local (Python 3.11): `uv venv --python 3.11 .venv && uv pip install -r requirements-dev.lock && .venv/bin/pytest`
(Windows: `.venv\Scripts\pytest`). Dependencies are declared in `pyproject.toml` and locked in `requirements.lock` / `requirements-dev.lock`
(`uv pip compile pyproject.toml -o requirements.lock --python 3.11 --universal`).

## Layout
```
app/api.py            FastAPI factory (auth, limits, routes, error mapping, request ids)
app/container.py      service wiring (all external clients; tests inject fakes here)
app/agents/           async LangGraph pipeline
app/guardrails/       NeMo rails (fail-closed) + injection screen
app/services/         llm (Portkey), embedding (Gemini), vectorstore (Qdrant), reranker (FlashRank), planner cache
app/auth/             API keys (sha256, Postgres) + CLI
app/db/               migrations, thread-TTL cleanup
app/ingestion/        loaders, chunker, idempotent blue/green pipeline, CLI
app/worker.py         arq worker (ingest jobs + nightly cleanup cron)
tests/                pytest suite (see tests/conftest.py for the harness)
evals/ ui/            RAGAS eval suite + Streamlit UI (send RAG_API_KEY as X-API-Key)
```

## Documentation index
Docs live in [`DOCS/`](DOCS/): [01 Overview](DOCS/01_SYSTEM_OVERVIEW.md) · [02 Ingestion](DOCS/02_INGESTION_ENGINE.md) ·
[03 Nodes](DOCS/03_NODE_INTELLIGENCE.md) · [04 Tracing](DOCS/04_TRACING_AND_OBSERVABILITY.md) ·
[05 Environment](DOCS/05_ENVIRONMENT_VARIABLES.md) · [06 Gotchas](DOCS/06_KNOWN_GOTCHAS.md) ·
[07 FlashRank](DOCS/07_FLASHRANK_RERANKING.md) · [08 Guardrails](DOCS/08_GUARDRAILS.md) ·
[09 LLM Gateway](DOCS/09_LLM_GATEWAY.md) · [10 Evals](DOCS/10_EVALS.md) · [11 Evals pipeline](DOCS/11_EVALS_PIPELINE.md).
Older sections of docs 01, 04, 06, 07, 10, 11 describe the prototype and may be stale; 02, 03, 05, 08, 09 carry current-state notes.

## Known limits
Docker/compose and Qdrant-server-only features (payload indexes, `is_tenant`) are validated only by config review here,
not by a live run. `evals/` still read `sources` as contexts (now file names) and need a follow-up.
