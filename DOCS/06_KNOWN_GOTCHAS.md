> **Current state / gotchas.** (1) Indexes built before `hybrid-v1` are refused at startup: run `--rebuild`. (2) Changing `EMBEDDING_MODEL`/`EMBEDDING_DIM` needs a rebuild
> (checked at startup and in `/readyz`). (3) The semantic cache keys on `index_version`; a rebuild invalidates it automatically. (4) Behind pgbouncer use transaction pooling
> and run migrations with `DIRECT_DATABASE_URL`. (5) On Windows `arq`'s `Worker.close()` needs SIGUSR1 (POSIX only). (6) Qdrant local mode (`:memory:`) ignores payload indexes and its `count()` is stale after filtered deletes; use a server for realistic checks.

# ⚠️ Known Gotchas & Architectural Decisions

This document tracks non-obvious platform quirks and explains why the architecture is designed the way it is.

---

## 1. Logfire Initialization Order (The "Poisoning" Bug)

**The Issue:**
When adding Logfire observability, you might see traces fail to appear in the dashboard, accompanied by the warning:
`No logs or spans will be created until logfire.configure() has been called.`

**The Reason:**
If any module in the application calls `logfire.info()` or `logfire.span()` *before* `logfire.configure()` has been executed, Logfire's internal state becomes "poisoned" for that process. It enters a silent, no-op mode and discards all subsequent traces, even if you call `.configure()` later.

If we were to import `settings` from `app.config` at the top of `app/main.py` to get the `LOGFIRE_TOKEN`, Python's import engine could inadvertently load nested modules (like our reranker or LLM clients) which might contain module-level Logfire calls, triggering this poisoning effect.

**The Solution:**
We bypass the `config.py` file entirely at the very top of `app/main.py`. We load the environment variables directly using `os.getenv()` and configure Logfire **before any other application imports occur**.

```python
# app/main.py
import logfire
import os
from dotenv import load_dotenv

load_dotenv()
logfire.configure(token=os.getenv("LOGFIRE_TOKEN"))

# Safe to import the rest of the application now!
from app.config import settings 
from app.agents.graph import rag_agent
```
This guarantees that Logfire is fully awake and tracing before the rest of the application is loaded into memory.

---

## 2. The "Lazy Loading" Pattern (FlashRank & Gemini)

**The Issue:**
Loading heavy machine learning models (like FlashRank) or initializing large SDKs (like the Gemini client) at the very top of your files can cause two major problems:
1.  **FastAPI Startup Delays**: The server won't start responding to health checks until the models are loaded.
2.  **Logfire Poisoning**: If these SDKs make any internal calls before Logfire is configured, they can "poison" the process, causing your traces to disappear.

**The Solution:**
We implemented a **Lazy Loading** pattern across the entire application. We do not initialize the Gemini embedding client or the FlashRank ranker at the module level. Instead, we wrap them in "getter" functions that only trigger the first time they are actually needed.

```python
# app/services/retrieval/embedding.py
def get_embedding_model():
    global model
    if model is None:
        # Initialized ONLY on first use!
        model = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-2-preview",
            google_api_key=settings.GEMINI_API_KEY
        )
    return model
```

This ensures your FastAPI server starts in **milliseconds**, and Logfire is guaranteed to be active before any AI service is touched.


## Behaviours added by the self-review
* **One in-flight request per session.** `/query` takes a Redis lock `lock:thread:<tenant:user:session>` (TTL = 4x `LLM_TIMEOUT_S`); a second concurrent request on the same session gets **409** with `Retry-After: 1`. Reason: with `durability="exit"` two concurrent runs would each write the checkpoint and the later one would silently drop the other's turn. If Redis is down the lock is skipped (logged).
* **Semantic cache keys on the user's own words** (never on the planner's refined query) and only for the **first turn** of a session; the LLM's `standalone` flag is ignored.
* **Request bodies over 64 KB** are rejected with 413 even when chunked or when `Content-Length` is missing/malformed.
* **A blue/green `--rebuild` copies other tenants' points at the start.** Writes to other tenants made while the rebuild runs go to the old collection and are not carried over; run rebuilds in a quiet window or re-ingest those tenants afterwards. Concurrent rebuilds of the same alias are not coordinated (the second fails on `create_collection`).
* Stale-point cleanup on re-ingest is scoped by `(tenant, source, source_type)`; the same relative filename under another `source_type` is a different file.
* Unauthenticated requests with random keys each cost one Postgres lookup (negative results are cached for `key_cache_ttl`, 10 s); put an IP-level limiter at the edge.
