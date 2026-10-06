"""Test harness: REAL Postgres (pgserver) + REAL redis protocol (fakeredis TCP server) + Qdrant local mode.
All LLMs / embeddings are deterministic fakes. No network, no paid APIs."""
from __future__ import annotations

import contextlib
import hashlib
import math
import re
import socket
import tempfile
import threading
import uuid
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fakeredis import TcpFakeServer
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from qdrant_client import AsyncQdrantClient

from app.api import create_app
from app.auth import keys
from app.config import Settings
from app.db.migrate import migrate
from app.guardrails import Guardrails
from app.resilience import Breakers
from app.services.llm import LLMResult
from app.services.reranker import NoopReranker

import asyncio
import sys

if sys.platform == "win32":  # psycopg async needs the selector loop (Linux/CI unaffected)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DIM = 128


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_url():
    import pgserver

    d = tempfile.mkdtemp(prefix="pgtest")
    srv = pgserver.get_server(d + "/pg", cleanup_mode="stop")
    url = srv.get_uri()
    migrate(url)
    yield url


@pytest.fixture(scope="session")
def redis_url():
    port = _free_port()
    server = TcpFakeServer(("127.0.0.1", port), server_type="redis")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder with the correct dimension."""

    model, dim = "fake-embed", DIM

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self.calls = 0
        self.fail: Exception | None = None

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail:
            raise self.fail
        return self._vec(text)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        if self.fail:
            raise self.fail
        return [self._vec(t) for t in texts]


@dataclass
class FakeLLM:
    """Fake chat model. Planner: greetings => CONVERSATIONAL else the question as the search query."""

    responder_text: str = "FAKE ANSWER"
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # (feature, system, user)

    async def complete(self, system, user, *, feature, temperature=0.0):
        self.calls.append((feature, system, user))
        if feature == "planner":
            msg = re.search(r"<<<(.*)>>>", user, re.S).group(1).strip()
            conv = re.fullmatch(r"(hi|hello|hey|thanks)[.! ]*", msg, re.I) or "my name" in msg.lower()
            return LLMResult("CONVERSATIONAL" if conv else msg)
        return LLMResult(self.responder_text)

    def of(self, feature):
        return [c for c in self.calls if c[0] == feature]


def make_guard(settings: Settings, responses=("No",)) -> Guardrails:
    g = Guardrails(settings, Breakers(5, 30))
    g.initialize(FakeListChatModel(responses=list(responses)))
    return g


@pytest.fixture
def settings(pg_url, redis_url):
    def _s(**kw) -> Settings:
        base = dict(
            app_env="dev", database_url=pg_url, redis_url=redis_url, qdrant_url=":memory:",
            qdrant_collection=f"t{uuid.uuid4().hex[:8]}", embedding_dim=DIM, embedding_model="fake-embed",
            default_rate_per_min=1000, ingest_root=tempfile.gettempdir(),
        )
        base.update(kw)
        return Settings(_env_file=None, **base)

    return _s


@dataclass
class Env:
    client: httpx.AsyncClient
    svc: object
    llm: FakeLLM
    embedder: FakeEmbedder
    settings: Settings


@pytest.fixture
def make_app(settings):
    @contextlib.asynccontextmanager
    async def _make(guard_responses=("No",), guard=None, qdrant_client=None, llm=None, embedder=None, **kw):
        s = settings(**kw)
        llm = llm or FakeLLM()
        embedder = embedder or FakeEmbedder()
        ov = {
            "llm": llm, "embedder": embedder, "reranker": NoopReranker(), "key_cache_ttl": 0.0,
            "qdrant_client": qdrant_client or AsyncQdrantClient(":memory:"),
            "guard": guard or make_guard(s, guard_responses),
        }
        app = create_app(s, ov)
        async with LifespanManager(app) as mgr:
            transport = httpx.ASGITransport(app=mgr.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                yield Env(c, app.state.svc, llm, embedder, s)

    return _make


@pytest.fixture
def new_key(pg_url):
    def _k(tenant: str | None = None, user: str = "u1", admin: bool = False, per_min: int = 1000, per_day: int = 100000):
        tenant = tenant or f"tn{uuid.uuid4().hex[:8]}"
        raw = keys.create_key(pg_url, tenant, user, is_admin=admin, rate_per_min=per_min, rate_per_day=per_day)
        return tenant, raw

    return _k


@pytest_asyncio.fixture
async def seed_docs(tmp_path):
    """Ingest {filename: text} for a tenant through the real pipeline."""
    from app.ingestion.pipeline import ingest

    async def _seed(env: Env, tenant: str, docs: dict[str, str], **kw):
        d = tmp_path / f"docs_{uuid.uuid4().hex[:6]}"
        d.mkdir()
        for name, text in docs.items():
            (d / name).write_text(text, encoding="utf-8")
        return await ingest(env.svc.vectors, env.embedder, env.settings, tenant, str(d), **kw)

    return _seed


def H(key: str) -> dict:
    return {"X-API-Key": key}
