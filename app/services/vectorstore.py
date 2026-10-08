"""Qdrant access: blue/green collections behind an alias, tenant-filtered search, config metadata."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import logfire
from qdrant_client import AsyncQdrantClient, models

from app.config import Settings
from app.errors import EmbeddingConfigMismatch, RetrievalUnavailable
from app.resilience import Breakers, call_upstream

_VER = re.compile(r"_v(\d+)$")


def version_of(collection: str) -> int:
    m = _VER.search(collection)
    return int(m.group(1)) if m else 0


def make_client(s: Settings) -> AsyncQdrantClient:
    if s.qdrant_url == ":memory:":
        return AsyncQdrantClient(location=":memory:")
    return AsyncQdrantClient(
        url=s.qdrant_url,
        api_key=s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None,
        timeout=int(max(1, s.qdrant_timeout_s)),
    )


SCHEMA = "hybrid-v1"  # named vectors: dense + sparse(IDF)


@dataclass
class Hit:
    text: str
    source: str
    score: float
    page: int | None = None
    heading_path: str = ""


class VectorStore:
    def __init__(self, client: AsyncQdrantClient, s: Settings, breakers: Breakers, coordinator=None):
        self.client, self.s, self._breaker = client, s, breakers["qdrant"]
        self.coordinator = coordinator  # RebuildCoordinator | None
        self.alias = s.qdrant_collection
        self._ver_cache: tuple[float, int] | None = None

    def _meta(self) -> dict:
        return {"embedding_model": self.s.embedding_model, "embedding_dim": self.s.embedding_dim, "schema": SCHEMA}

    async def active_version(self) -> int:
        """Version number of the live collection (cached ~10s so a rebuild is picked up quickly)."""
        now = time.monotonic()
        if self._ver_cache and now - self._ver_cache[0] < 10:
            return self._ver_cache[1]
        cur = await self.resolve_alias()
        v = int(_VER.search(cur).group(1)) if cur and _VER.search(cur) else 0
        self._ver_cache = (now, v)
        return v

    async def _q(self, coro_fn):
        return await call_upstream(
            self._breaker, coro_fn, timeout_s=self.s.qdrant_timeout_s, attempts=self.s.max_attempts, wait_max_s=1.0
        )

    # ---- schema / blue-green ----------------------------------------------------------------
    async def resolve_alias(self) -> str | None:
        aliases = (await self.client.get_aliases()).aliases
        return next((a.collection_name for a in aliases if a.alias_name == self.alias), None)

    async def versions(self) -> list[int]:
        cols = (await self.client.get_collections()).collections
        own = re.compile(rf"^{re.escape(self.alias)}_v(\d+)$")  # exact: never count another alias that merely shares a prefix
        return sorted(int(m.group(1)) for c in cols if (m := own.match(c.name)))

    async def create_versioned(self, version: int) -> str:
        name = f"{self.alias}_v{version}"
        await self.client.create_collection(
            collection_name=name,
            vectors_config={"dense": models.VectorParams(size=self.s.embedding_dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            metadata=self._meta(),
        )
        await self.client.create_payload_index(
            name, "tenant_id", field_schema=models.KeywordIndexParams(type="keyword", is_tenant=True)
        )
        await self.client.create_payload_index(name, "source_type", field_schema=models.PayloadSchemaType.KEYWORD)
        await self.client.create_payload_index(name, "source", field_schema=models.PayloadSchemaType.KEYWORD)
        await self.client.create_payload_index(name, "file_hash", field_schema=models.PayloadSchemaType.KEYWORD)
        return name

    async def swap_alias(self, new_collection: str) -> None:
        ops = []
        if await self.resolve_alias():
            ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=self.alias)))
        ops.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=new_collection, alias_name=self.alias)
            )
        )
        await self.client.update_collection_aliases(change_aliases_operations=ops)  # applied atomically
        self._ver_cache = None

    async def ensure_active(self) -> str:
        """Return the active physical collection, creating v1 + alias on first run."""
        current = await self.resolve_alias()
        if current:
            return current
        name = await self.create_versioned(1)
        await self.swap_alias(name)
        return name

    # ---- writes (mirrored to the rebuild candidate while a rebuild is active) ---------------------
    async def _mirror(self, target: str):
        if not self.coordinator:
            return None
        cand = await self.coordinator.mirror_target(target)
        return cand if cand and cand != target else None

    async def write_points(self, target: str, points: list) -> None:
        await self.client.upsert(target, points, wait=True)
        cand = await self._mirror(target)
        if cand:
            try:
                await self.client.upsert(cand, points, wait=True)
            except Exception as e:  # never fail the primary write; the pre-swap reconciliation repairs drift
                logfire.warn("dual-write upsert to rebuild candidate failed", candidate=cand, error=str(e))

    async def delete_where(self, target: str, selector) -> None:
        await self.client.delete(target, points_selector=selector, wait=True)
        cand = await self._mirror(target)
        if cand:
            try:
                await self.client.delete(cand, points_selector=selector, wait=True)
            except Exception as e:
                logfire.warn("dual-write delete on rebuild candidate failed", candidate=cand, error=str(e))

    async def gc_orphans(self, live: str) -> list[str]:
        """Delete candidates left behind by crashed rebuilds (version above the live one). Call only while holding the rebuild lock."""
        live_v = version_of(live)
        gone = []
        for v in await self.versions():
            if v > live_v:
                name = f"{self.alias}_v{v}"
                await self.client.delete_collection(name)
                gone.append(name)
        if gone:
            logfire.warn("garbage-collected orphaned rebuild candidates", collections=gone)
        return gone

    async def prune_old(self, active: str) -> None:
        keep = self.s.keep_old_collections
        vs = await self.versions()
        act = int(_VER.search(active).group(1))
        for v in [x for x in vs if x < act][: max(0, len([x for x in vs if x < act]) - keep)]:
            await self.client.delete_collection(f"{self.alias}_v{v}")

    async def verify_config(self) -> None:
        """Refuse to start if the index was built with a different embedding model/dimension."""
        current = await self.resolve_alias()
        if not current:
            return  # empty system: first ingestion creates the collection with the configured model
        info = await self.client.get_collection(current)
        vec = info.config.params.vectors
        vec = vec.get("dense") if isinstance(vec, dict) else vec
        dim = vec.size if hasattr(vec, "size") else None
        meta = getattr(info.config, "metadata", None) or {}
        want = self._meta()
        if dim != want["embedding_dim"] or meta.get("embedding_model") != want["embedding_model"] or meta.get("embedding_dim") != want["embedding_dim"] or meta.get("schema") != SCHEMA:
            raise EmbeddingConfigMismatch(
                f"Index '{current}' was built with model={meta.get('embedding_model')} dim={meta.get('embedding_dim') or dim} schema={meta.get('schema')}, "
                f"but config says model={want['embedding_model']} dim={want['embedding_dim']}. Rebuild the index (--rebuild) or fix config."
            )

    # ---- search -----------------------------------------------------------------------------
    async def search(self, tenant_id: str, dense: list[float], sparse: models.SparseVector,
                     prefetch_k: int, fused_k: int) -> list[Hit]:
        """Hybrid: dense + sparse prefetch (tenant-filtered) -> server-side RRF fusion."""
        flt = models.Filter(must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))])
        try:
            resp = await self._q(
                lambda: self.client.query_points(
                    collection_name=self.alias,
                    prefetch=[
                        models.Prefetch(query=dense, using="dense", filter=flt, limit=prefetch_k),
                        models.Prefetch(query=sparse, using="sparse", filter=flt, limit=prefetch_k),
                    ],
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    query_filter=flt,
                    limit=fused_k,
                    with_payload=True,
                )
            )
        except Exception as e:
            logfire.error("qdrant search failed", error=str(e))
            raise RetrievalUnavailable(str(e)) from e
        return [
            Hit(p.payload.get("text", ""), p.payload.get("source", "unknown"), p.score,
                p.payload.get("page"), p.payload.get("heading_path", ""))
            for p in resp.points
            if p.payload and p.payload.get("tenant_id") == tenant_id  # defence in depth
        ]

    async def ping(self) -> None:
        await self.client.get_collections()
