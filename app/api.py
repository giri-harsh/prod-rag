"""FastAPI app factory: auth, tenant-scoped sessions, rate limits, guarded RAG, ops endpoints."""
from __future__ import annotations

import contextlib
import re
import uuid
from contextvars import ContextVar
from pathlib import Path

import logfire
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, ConfigDict

from app.auth.keys import Principal
from app.config import Settings, load_settings
from app.container import Services, build_services
from app.errors import AppError, EmbeddingConfigMismatch
from app.observability.metrics import UPSTREAM_ERRORS
from app.observability.redact import install_logging_filter, langsmith_client, logfire_scrubbing
from app.ratelimit import Limited


class BodyLimitMiddleware:
    """Reject request bodies over `limit` bytes with 413 - counts streamed/chunked bodies too, not just Content-Length."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope["headers"]:
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.limit:
                    return await self._reject(send)
        seen, too_big = 0, False

        async def limited_receive():
            nonlocal seen, too_big
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    too_big = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        started = False

        async def guarded_send(msg):
            nonlocal started
            if too_big and not started:
                started = True
                return await self._reject(send)
            if too_big:
                return None
            started = started or msg["type"] == "http.response.start"
            return await send(msg)

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _reject(send):
        body = b'{"error":"payload too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY_BYTES = 64 * 1024
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # extras inspected explicitly so thread_id gets a clear 400

    q: str
    session_id: str = "default"
    tenant_id: str | None = None  # optional assertion; must equal the key's tenant
    include_contexts: bool = False  # chunk texts in the response; requires the "eval" key scope


class IngestRequest(BaseModel):
    tenant_id: str
    path: str  # relative to INGEST_ROOT
    source_type: str | None = None
    rebuild: bool = False


class SessionBusy(AppError):
    status_code = 409
    public_message = "Another request for this session is still running; retry shortly"
    retry_after = 1


async def acquire_thread_lock(svc, thread_id: str):
    """Serialise requests per conversation: with durability="exit" a concurrent second request would otherwise
    overwrite the first one's history (last writer wins). Redis outage => proceed unlocked (logged)."""
    key, token = f"lock:thread:{thread_id}", uuid.uuid4().hex
    try:
        ok = await svc.redis.set(key, token, nx=True, ex=int(svc.settings.llm_timeout_s * 4))
    except Exception as e:
        logfire.warn("thread lock unavailable; proceeding without it", error=str(e))
        return None
    if not ok:
        raise SessionBusy(thread_id)
    return key, token


async def release_thread_lock(svc, lock) -> None:
    if not lock:
        return
    key, token = lock
    try:
        cur = await svc.redis.get(key)
        if (cur.decode() if isinstance(cur, bytes) else cur) == token:
            await svc.redis.delete(key)
    except Exception as e:
        logfire.warn("thread lock release failed (expires on its own)", error=str(e))


def create_app(settings: Settings | None = None, overrides: dict | None = None) -> FastAPI:
    s = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = await build_services(s, overrides)
        app.state.svc = svc
        if s.app_env != "test":
            try:
                from arq import create_pool
                from arq.connections import RedisSettings

                svc.arq = await create_pool(RedisSettings.from_dsn(s.redis_url))
                svc._closers.append(svc.arq.aclose)
            except Exception as e:
                logfire.warn("arq pool unavailable; /ingest disabled", error=str(e))
        try:
            yield
        finally:
            await svc.aclose()

    logfire.configure(
        send_to_logfire="if-token-present", console=False, service_name="rag-api", scrubbing=logfire_scrubbing(),
        token=s.logfire_token.get_secret_value() if s.logfire_token else None,
    )
    install_logging_filter()
    ls_client = langsmith_client() if s.langsmith_tracing else None
    app = FastAPI(title="Enterprise RAG API", version="0.2.0", lifespan=lifespan)
    app.state.metrics_registry = CollectorRegistry()
    Instrumentator(registry=app.state.metrics_registry, excluded_handlers=["/healthz", "/readyz"]).instrument(app)
    with contextlib.suppress(Exception):
        logfire.instrument_fastapi(app, capture_headers=False)

    # ---- middleware -------------------------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rid):
            rid = uuid.uuid4().hex
        tok = request_id_var.set(rid)
        try:
            with logfire.set_baggage(request_id=rid), logfire.span(
                "{method} {path}", method=request.method, path=request.url.path, request_id=rid
            ):
                resp: Response = await call_next(request)
        finally:
            request_id_var.reset(tok)
        resp.headers["X-Request-ID"] = rid
        return resp

    # ---- error mapping ----------------------------------------------------------------------
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        UPSTREAM_ERRORS.labels(type(exc).__name__).inc()
        logfire.error("request failed", error_type=type(exc).__name__, detail=str(exc))
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
        return JSONResponse({"error": exc.public_message, "request_id": request_id_var.get()}, exc.status_code, headers)

    @app.exception_handler(Limited)
    async def _limited(request: Request, exc: Limited):
        return JSONResponse(
            {"error": f"rate limit exceeded ({exc.window})", "request_id": request_id_var.get()},
            429, {"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logfire.error("unhandled exception", error=repr(exc))
        return JSONResponse({"error": "internal server error", "request_id": request_id_var.get()}, 500)

    # ---- dependencies -----------------------------------------------------------------------
    def svc_of(request: Request) -> Services:
        return request.app.state.svc

    async def authenticate(request: Request) -> Principal:
        raw = request.headers.get("x-api-key")
        if not raw:
            raise HTTPException(401, "missing API key", headers={"WWW-Authenticate": "ApiKey"})
        p = await svc_of(request).keystore.lookup(raw)
        if p is None:
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "ApiKey"})
        request.state.principal = p
        return p

    async def limited(request: Request, p: Principal = Depends(authenticate)) -> Principal:
        await svc_of(request).limiter.check(p)  # raises Limited -> 429
        return p

    async def admin(request: Request, p: Principal = Depends(limited)) -> Principal:
        if not p.is_admin:
            raise HTTPException(403, "admin key required")
        return p

    # ---- routes -----------------------------------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        """Probe-friendly: only ready/not-ready. Per-dependency detail is shown to admin keys only."""
        svc = svc_of(request)
        checks: dict[str, str] = {}

        async def probe(name, coro):
            try:
                await coro
                checks[name] = "ok"
            except Exception as e:
                checks[name] = f"fail: {type(e).__name__}"

        async def pg():
            async with svc.pool.connection() as c:
                await c.execute("SELECT 1 FROM api_keys LIMIT 1")

        await probe("postgres", pg())
        await probe("redis", svc.redis.ping())
        await probe("qdrant", svc.vectors.ping())
        await probe("embedding_config", svc.vectors.verify_config())
        checks["guardrails"] = "ok" if svc.guard.ready else "fail: not initialised"
        ok = all(v == "ok" for v in checks.values())
        body: dict = {"ready": ok}
        raw = request.headers.get("x-api-key")
        if raw:
            p = await svc.keystore.lookup(raw)
            if p and p.is_admin:
                body["checks"] = checks
                if not svc.guard.ready:
                    body["guardrails_error"] = svc.guard.init_error
        return JSONResponse(body, 200 if ok else 503)

    @app.post("/query")
    async def query(body: QueryRequest, request: Request, p: Principal = Depends(limited)):
        svc = svc_of(request)
        extra = body.model_extra or {}
        if "thread_id" in extra:
            raise HTTPException(400, "thread_id is server-derived; send only session_id")
        if body.tenant_id is not None and body.tenant_id != p.tenant_id:
            raise HTTPException(403, "cross-tenant access denied")
        if not SESSION_RE.match(body.session_id):
            raise HTTPException(422, "session_id must match [A-Za-z0-9_-]{1,64}")
        q = body.q.strip()
        if not q:
            raise HTTPException(422, "q must not be empty")
        if len(q) > svc.settings.max_query_chars:
            raise HTTPException(413, f"q exceeds {svc.settings.max_query_chars} characters")

        if body.include_contexts and not p.has_scope("eval"):
            raise HTTPException(403, "include_contexts requires the 'eval' scope")
        thread_id = f"{p.tenant_id}:{p.user_id}:{body.session_id}"
        lock = await acquire_thread_lock(svc, thread_id)  # 409 if another request is mid-flight on this session
        try:
            return await _run_query(svc, p, body, q, thread_id, ls_client)
        finally:
            await release_thread_lock(svc, lock)

    async def _run_query(svc, p, body, q, thread_id, ls_client):
        async with svc.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO thread_activity (thread_id, tenant_id) VALUES (%s,%s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_active = now()",
                (thread_id, p.tenant_id),
            )
        # durability="exit": one checkpoint write per request instead of one per node (load test: the per-step
        # writes through the saver's single connection lock were the first bottleneck)
        cfg = {"configurable": {"thread_id": thread_id, "tenant_id": p.tenant_id}}
        if ls_client is not None:
            from langsmith import tracing_context

            with tracing_context(client=ls_client, enabled=True):
                out = await svc.graph.ainvoke({"messages": [{"role": "user", "content": q}]}, config=cfg, durability="exit")
        else:
            out = await svc.graph.ainvoke({"messages": [{"role": "user", "content": q}]}, config=cfg, durability="exit")
        if not out.get("output_checked"):  # defence in depth: never return an unchecked answer
            raise AppError("output guard did not run")
        resp = {
            "answer": out["final_answer"],
            "session_id": body.session_id,
            "sources": out.get("sources", []),
            "thought_process": out.get("plan", []),
            "status": out.get("status"),
            "blocked": bool(out.get("blocked")),
            "cache_hit": bool(out.get("cache_hit")),
            "iterations": out.get("iteration", 0),
        }
        if body.include_contexts:
            resp["contexts"] = out.get("documents", [])
        return resp

    @app.post("/ingest", status_code=202)
    async def ingest(body: IngestRequest, request: Request, p: Principal = Depends(admin)):
        svc = svc_of(request)
        if body.tenant_id != p.tenant_id and not p.has_scope("platform"):
            raise HTTPException(403, "an admin key may only ingest into its own tenant (platform scope required otherwise)")
        root = Path(svc.settings.ingest_root).resolve()
        target = (root / body.path).resolve()
        if root != target and root not in target.parents:
            raise HTTPException(400, "path escapes INGEST_ROOT")
        if not target.exists():
            raise HTTPException(404, "path not found")
        if svc.arq is None:
            raise HTTPException(503, "job queue unavailable")
        job = await svc.arq.enqueue_job("ingest_job", body.tenant_id, str(target), body.source_type, body.rebuild)
        return {"job_id": job.job_id if job else None, "status": "queued"}

    @app.get("/graph", response_class=PlainTextResponse)
    async def graph(request: Request, p: Principal = Depends(admin)):
        return svc_of(request).graph.get_graph().draw_mermaid()

    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)  # added last = outermost: runs before anything reads the body
    return app


__all__ = ["create_app", "EmbeddingConfigMismatch"]
