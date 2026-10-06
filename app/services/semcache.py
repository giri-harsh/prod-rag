"""Semantic answer cache in Qdrant (one collection, partitioned by tenant_id payload with is_tenant index).

Rules (enforced by callers in the graph, and by the key design here):
* hit only when similarity >= threshold (0.95) AND same tenant AND same index_version AND not expired;
* index_version is part of the match, so a re-index (new collection version) invalidates everything;
* only standalone, unblocked, grounded answers are stored (never refusals or "no context");
* expiry: `created_at` is checked at read time and purged by the `semcache_cleanup` cron.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass

import logfire
from prometheus_client import Counter
from qdrant_client import AsyncQdrantClient, models

from app.config import Settings

SEMCACHE = Counter("rag_semcache_total", "Semantic cache events", ["result"])
_NS = uuid.UUID("0b7f6f0e-5a0b-4c3a-8f0e-2d7a9c1e4b55")


@dataclass
class CachedAnswer:
    answer: str
    sources: list[str]
    score: float


class SemanticCache:
    def __init__(self, client: AsyncQdrantClient, s: Settings):
        self.client, self.s = client, s
        self.collection = f"{s.qdrant_collection}_semcache"
        self.ttl = s.semcache_ttl_s
        self.threshold = s.semcache_threshold
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return
        if not await self.client.collection_exists(self.collection):
            try:
                await self.client.create_collection(
                    self.collection,
                    vectors_config=models.VectorParams(size=self.s.embedding_dim, distance=models.Distance.COSINE),
                )
                await self.client.create_payload_index(
                    self.collection, "tenant_id", field_schema=models.KeywordIndexParams(type="keyword", is_tenant=True)
                )
                await self.client.create_payload_index(self.collection, "index_version", field_schema=models.PayloadSchemaType.INTEGER)
                await self.client.create_payload_index(self.collection, "created_at", field_schema=models.PayloadSchemaType.INTEGER)
            except Exception as e:  # another worker may have created it concurrently
                if not await self.client.collection_exists(self.collection):
                    raise e
        self._ready = True

    def _filter(self, tenant_id: str, version: int) -> models.Filter:
        return models.Filter(must=[
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
            models.FieldCondition(key="index_version", match=models.MatchValue(value=version)),
            models.FieldCondition(key="created_at", range=models.Range(gte=int(time.time()) - self.ttl)),
        ])

    async def lookup(self, tenant_id: str, vector: list[float], version: int) -> CachedAnswer | None:
        try:
            await self.ensure()
            r = await self.client.query_points(
                self.collection, query=vector, query_filter=self._filter(tenant_id, version),
                score_threshold=self.threshold, limit=1, with_payload=True,
            )
        except Exception as e:
            SEMCACHE.labels("error").inc()
            logfire.warn("semcache lookup failed", error=str(e))
            return None
        if not r.points:
            SEMCACHE.labels("miss").inc()
            return None
        p = r.points[0]
        SEMCACHE.labels("hit").inc()
        return CachedAnswer(p.payload["answer"], p.payload.get("sources", []), p.score)

    async def store(self, tenant_id: str, query: str, vector: list[float], version: int,
                    answer: str, sources: list[str]) -> None:
        norm = re.sub(r"\s+", " ", query.strip().lower())
        pid = str(uuid.uuid5(_NS, f"{tenant_id}|{version}|{hashlib.sha256(norm.encode()).hexdigest()}"))
        try:
            await self.ensure()
            await self.client.upsert(self.collection, [models.PointStruct(
                id=pid, vector=vector,
                payload={"tenant_id": tenant_id, "index_version": version, "created_at": int(time.time()),
                         "query": query[:500], "answer": answer, "sources": sources},
            )])
            SEMCACHE.labels("store").inc()
        except Exception as e:
            SEMCACHE.labels("error").inc()
            logfire.warn("semcache store failed", error=str(e))

    async def purge_expired(self) -> None:
        if not await self.client.collection_exists(self.collection):
            return
        await self.client.delete(
            self.collection,
            points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="created_at", range=models.Range(lt=int(time.time()) - self.ttl))
            ])),
            wait=True,
        )
