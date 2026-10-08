# Gotchas and non-obvious behaviours

## Operating
* **Index schema**: indexes built before `hybrid-v1` (no named vectors) are refused at startup with a message to rebuild. Changing `EMBEDDING_MODEL`/`EMBEDDING_DIM` also needs `--rebuild` (checked at startup and in `/readyz`). The semantic cache keys on `index_version`, so a rebuild invalidates it automatically.
* **pgbouncer**: use transaction pooling for the app (`prepare_threshold=0` is set) and run migrations with `DIRECT_DATABASE_URL` (session mode, advisory lock).
* **One in-flight request per session**: `/query` takes a Redis lock `lock:thread:<tenant:user:session>` (TTL = 4x `LLM_TIMEOUT_S`); a second concurrent request on the same session gets **409** (`Retry-After: 1`). With `durability="exit"` two concurrent runs would otherwise overwrite each other's turn. If Redis is down the lock is skipped (logged).
* **Semantic cache** keys on the user's own words (never the planner's refined query) and only for the first turn of a session; the LLM's `standalone` flag is ignored. Blocked answers, refusals and "no context" answers are never cached.
* **Request bodies over 64 KB** get 413, including chunked bodies and malformed `Content-Length`.
* **Rebuilds** are coordinated (one per alias, dual-write, reconciliation; see [02](02_INGESTION_ENGINE.md)). Cost: a rebuild copies every other tenant's points.
* Stale-point cleanup on re-ingest is scoped by `(tenant, source, source_type)`: the same relative filename under another `source_type` is a different file.
* **Auth lookup cost**: `X-API-Key` goes through an in-process LRU of valid keys (`KEY_CACHE_TTL_S`=30) -> Redis negative cache of unknown hashes (`KEY_NEG_TTL_S`=60) -> Postgres. `auth.cli revoke`/`create` publish on Redis channel `auth:evict` so every worker drops the key immediately; direct SQL revocation waits for the TTL.
  Failed authentications are counted per client IP per minute (`AUTH_FAIL_PER_MIN`=20) and then answered with 429. `X-Forwarded-For` is honoured only from `TRUSTED_PROXIES` (right-most untrusted hop); set it to your ingress addresses or every client looks like the proxy.
* **Guardrails fail closed**: an uninitialised or erroring rail => 503 with `Retry-After`; `GUARD_ENGINE=actions` (default) calls NeMo's self-check actions directly (~1 ms CPU), `runtime` uses the full Colang engine (~20 ms).

## Development / platform
* Logfire calls made before `logfire.configure()` are dropped with a `LogfireNotConfiguredWarning` (visible in CLIs/scripts that call into the app first). `create_app()` configures Logfire before building services.
* psycopg's async mode needs a selector event loop on Windows (the test harness sets it; Linux is unaffected).
* arq's `Worker.close()` references `SIGUSR1` (POSIX only), so the arq round-trip test skips `close()` on Windows.
* Qdrant local mode (`:memory:`) ignores payload indexes and its `count()` is stale after filtered deletes; use a server for realistic checks. `fakeredis`' TCP server cannot run arq and does not deliver pub/sub, so those two tests need `REDIS_INTEGRATION_URL`.
* Heavy models are loaded during startup (`build_services`: FlashRank and NeMo rails in worker threads), so a worker is not ready until they are; `/readyz` reports it. Startup time was not measured.
* Under `APP_ENV=loadtest` the LLM, embedder and guard model are fakes with simulated latency (real datastores).
* **Windows + `localhost`**: libpq tries `::1` before `127.0.0.1`; if the database only listens on IPv4 each connect can stall for the full connect timeout (observed: 10 s). Use `127.0.0.1` in `DATABASE_URL`/`REDIS_URL`/`QDRANT_URL` on such hosts (the app's pool uses `connect_timeout=5`).
