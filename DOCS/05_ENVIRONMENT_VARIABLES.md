# Environment Variables & Configuration

All settings are validated at startup by `app/config.py` (pydantic-settings). Missing/invalid values fail fast with one
readable message (e.g. `DATABASE_URL: Field required`). `APP_ENV=prod` additionally requires the three API keys.
See `.env.example` for a template.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `dev` | `dev`, `test` (MemorySaver allowed ONLY here) or `prod` |
| `DATABASE_URL` | required | Postgres: API keys, conversation checkpoints, thread activity |
| `REDIS_URL` | required | Rate limits, planner cache, arq job queue |
| `QDRANT_URL` / `QDRANT_API_KEY` | required / - | Qdrant endpoint. `:memory:` allowed outside prod (tests) |
| `QDRANT_COLLECTION` | `enterprise_rag` | Alias name; physical collections are `<alias>_v<N>` |
| `GROQ_API_KEY`, `PORTKEY_API_KEY` | - | LLM access (guard model direct via Groq, chat via Portkey) |
| `GROQ_SLUG`, `GROQ_SLUG_FALLBACK` | `rag`, `brag` | Portkey virtual-key slugs for primary / fallback targets |
| `LLM_PRIMARY_MODEL`, `LLM_FALLBACK_MODEL`, `GUARD_MODEL` | llama-3.3-70b / 3.1-8b / 3.1-8b | Models |
| `GEMINI_API_KEY` | - | Embeddings |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `gemini-embedding-2` / `768` | GA model + explicit `output_dimensionality`; stored in collection metadata and verified at startup |
| `LLM_TIMEOUT_S` / `EMBED_TIMEOUT_S` / `QDRANT_TIMEOUT_S` | 30 / 10 / 5 | Per-call timeouts |
| `MAX_ATTEMPTS` | 3 | tenacity attempts (exp. backoff + jitter; 429/5xx/timeouts) |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | 5 / 30 | Circuit breaker per upstream |
| `LLM_CONCURRENCY` | 32 | Semaphore bounding in-flight LLM calls per worker |
| `DEFAULT_RATE_PER_MIN` / `DEFAULT_RATE_PER_DAY` | 60 / 10000 | Defaults for new keys (stored per key in Postgres) |
| `MAX_QUERY_CHARS` | 4000 | Query size limit (413 above) |
| `MEMORY_TTL_DAYS` | 30 | Idle threads older than this are deleted by the nightly arq cron |
| `INGEST_BATCH_SIZE` / `INGEST_ROOT` | 128 / `DATA` | Upsert batch size; root under which `/ingest` paths must live |
| `KEEP_OLD_COLLECTIONS` | 1 | Previous index versions retained after a rebuild |
| `ASSISTANT_DOMAIN` | K8s/Intel/networking | Topic policy used by the NeMo input rail prompt |
| `LOGFIRE_TOKEN` | - | Logfire export (optional) |
| `RAG_API_KEY` | - | Used by `ui/` and `evals/` clients (sent as `X-API-Key`) |

`GROQ_FALLBACK_API_KEY`, `LANGSMITH_*` and `BACKEND_URL` from the old setup are no longer read by the API.
