# Architecture

```mermaid
graph LR
  subgraph clients
    UI[Streamlit UI / API clients]
  end
  subgraph api[API pods - gunicorn + uvicorn workers]
    MW[auth, rate limit, body limit, request id] --> G[LangGraph pipeline]
    G --> R[FlashRank pool]
    G --> NG[NeMo self-check rails]
  end
  W[arq worker: ingestion jobs + cleanup crons]
  UI --> MW
  G --> PG[(Postgres: api_keys, tenants, checkpoints)]
  G --> RD[(Redis: limits, caches, locks, jobs, pub/sub)]
  G --> QD[(Qdrant: hybrid index, semantic cache)]
  G --> PK[Portkey -> Groq]
  G --> GM[Gemini embeddings]
  W --> QD
  W --> RD
  W --> GM
  PGB[pgbouncer] --- PG
  api --- PGB
```

| concern | where |
|---|---|
| authentication, tenancy | `app/api.py`, `app/auth/` (hashed keys, scopes, per-tenant domain) |
| retrieval | `app/services/vectorstore.py` (named vectors, RRF, tenant filter), `sparse.py`, `reranker.py` |
| orchestration | `app/agents/graph.py` (cycle: retrieve -> grade -> rewrite) |
| safety | `app/guardrails/` (input/output rails, injection screen, deterministic output screen) |
| caches | planner cache, embedding cache, semantic answer cache (`app/services/`) |
| ingestion | `app/ingestion/` (idempotent, batched, blue/green with lock + dual-write + reconciliation) |
| ops | `/healthz`, `/readyz`, internal Prometheus port, Logfire with PII scrubbing, `deploy/k8s/base`, `docker-compose.yml` |

Details: `DOCS/01`..`DOCS/12`. Capacity numbers and their caveats: `DOCS/12_CAPACITY.md`.
