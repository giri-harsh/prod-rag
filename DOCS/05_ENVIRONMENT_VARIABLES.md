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
| `LLM_PRIMARY_MODEL`, `LLM_FALLBACK_MODEL`, `GUARD_MODEL` | `openai/gpt-oss-120b` / `openai/gpt-oss-20b` / `openai/gpt-oss-20b` | Responder / cheap model (rewrite, planner, gateway fallback) / guard classifier. Providers retire models: these defaults named the llama-3.x line after Groq removed it, and the rails fail closed, so a stale name is a 503 on every request. `scripts/check_keys.py` verifies each one against the live account and lists replacements |
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
| `ASSISTANT_DOMAIN` | see `Settings.assistant_domain` | Topic policy used by the NeMo input rail prompt. It must name every component the corpus documents — the rail refuses what this string does not cover, so a gap here refuses questions the index can answer (see [08_GUARDRAILS.md](08_GUARDRAILS.md#calibration)) |
| `LOGFIRE_TOKEN` | - | Logfire export (optional) |
| `RAG_API_KEY` | - | Used by `ui/` and `evals/` clients (sent as `X-API-Key`) |

`GROQ_FALLBACK_API_KEY`, `LANGSMITH_*` and `BACKEND_URL` from the old setup are no longer read by the API.

## Added in the hardening phase
| Variable | Default | Purpose |
|---|---|---|
| `PREFETCH_K` / `FUSED_K` / `FINAL_K` | 40 / 10 / 5 | Per-branch candidates / after RRF / after rerank |
| `RERANK_MIN_SCORE` | 0.05 | Below this top score the retrieval is graded poor (rewrite + retry) |
| `MAX_RETRIEVAL_ITERATIONS` | 2 | Retrieval passes per request |
| `SPARSE_BACKEND` | `bm25` | `bm25` (fastembed `Qdrant/bm25`) or `simple` (built-in tokenizer; Qdrant IDF modifier) |
| `CHUNK_TARGET_TOKENS` / `CHUNK_OVERLAP_RATIO` / `CHUNK_MIN_TOKENS` | 900 / 0.15 / 200 | Chunker (tokens estimated as chars/4) |
| `EMBED_CACHE_TTL_S` | 604800 | Redis embedding cache TTL (7 days) |
| `SEMCACHE_ENABLED` / `SEMCACHE_THRESHOLD` / `SEMCACHE_TTL_S` | true / 0.95 / 86400 | Semantic answer cache |
| `WEB_CONCURRENCY` | 1 | Workers per pod; divides per-key limits in the Redis-down fallback |
| `PG_POOL_MAX` | 10 | Connections per worker process |
| `DIRECT_DATABASE_URL` | - | Session-mode URL for migrations when `DATABASE_URL` is pgbouncer |
| `METRICS_PORT` | 9100 | Internal Prometheus port |
| `GUARD_ENGINE` | `actions` | `actions` (direct NeMo self-check actions) or `runtime` (full Colang flow engine) |
| `LANGSMITH_TRACING` | false | Trace graph runs to LangSmith (PII-redacted) |
| `LOADTEST_*_MS` | 800/300, 80/30, 250 | `APP_ENV=loadtest` simulated LLM / embed / guard latency |
Per-tenant assistant domain: `python -m app.auth.cli tenant-set --tenant T --domain "..."` (falls back to `ASSISTANT_DOMAIN`).

## Added with the close-out phase
| Variable | Default | Purpose |
|---|---|---|
| `REBUILD_LOCK_TTL_S` | 60 | Rebuild lock/state TTL; a heartbeat renews it every TTL/3 while the rebuild is alive, so expiry means the holder died |
| `KEY_CACHE_TTL_S` / `KEY_CACHE_SIZE` | 30 / 10000 | In-process LRU of valid API keys (revocation also evicts instantly via Redis pub/sub `auth:evict`) |
| `KEY_NEG_TTL_S` | 60 | Redis negative cache of unknown key hashes |
| `AUTH_FAIL_PER_MIN` | 20 | Failed authentications per client IP per minute before 429 |
| `TRUSTED_PROXIES` | (empty) | Comma-separated IPs/CIDRs allowed to set `X-Forwarded-For` |
| `RERANK_WORKERS` | 2 | Threads in the dedicated reranker pool (max concurrent rerank batches per process) |
| `RERANK_THREADS` | 1 | ONNX Runtime intra-op threads per batch (workers x threads = reranker CPU threads per process) |
| `RERANK_MAX_LENGTH` | 512 | Passages are truncated to this many tokens (and pre-cut by characters) before scoring |
| `RERANK_TIMEOUT_S` | 10 | Queue wait + scoring; on timeout the fused order is used (score 1.0) |
| `FLASHRANK_CACHE` | `/opt/flashrank` | Directory holding the baked FlashRank ONNX model |
| `LOADTEST_REAL_RERANKER` | false | With `APP_ENV=loadtest`, use the real reranker instead of the no-op |
