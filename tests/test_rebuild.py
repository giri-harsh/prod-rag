"""O-1: rebuild lock, dual-write during rebuild, crash recovery, reconciliation."""
import asyncio
import uuid

import pytest
from qdrant_client import models

import app.ingestion.pipeline as P
from app.ingestion.pipeline import _scan_hashes, ingest, reconcile
from app.services.rebuild import RebuildInProgress
from tests.conftest import H

TEXT = "kubernetes pods are scheduled onto nodes by the scheduler. " * 8


async def points_of(e, collection, tenant):
    flt = models.Filter(must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant))])
    pts, _ = await e.svc.vectors.client.scroll(collection, scroll_filter=flt, limit=500, with_payload=True)
    return pts


def mk(tmp_path, name, files):
    d = tmp_path / f"{name}_{uuid.uuid4().hex[:5]}"
    d.mkdir()
    for fn, txt in files.items():
        (d / fn).write_text(txt, encoding="utf-8")
    return str(d)


async def run_ingest(e, tenant, path, **kw):
    return await ingest(e.svc.vectors, e.embedder, e.svc.sparse, e.settings, tenant, path, **kw)


# ----------------------------------------------------------------------------- lock
async def test_concurrent_rebuild_fails_fast(make_app, tmp_path):
    ta = f"a{uuid.uuid4().hex[:5]}"
    d = mk(tmp_path, "a", {"a.txt": TEXT})
    async with make_app() as e:
        await run_ingest(e, ta, d)  # creates v1
        gate = asyncio.Event()
        orig = e.embedder.embed_texts

        async def slow(texts):
            await gate.wait()
            return await orig(texts)

        e.embedder.embed_texts = slow
        t1 = asyncio.create_task(run_ingest(e, ta, d, rebuild=True))
        for _ in range(100):
            if await e.svc.vectors.coordinator.is_locked():
                break
            await asyncio.sleep(0.05)
        assert await e.svc.vectors.coordinator.is_locked()
        with pytest.raises(RebuildInProgress):
            await run_ingest(e, ta, d, rebuild=True)  # second rebuild fails fast, live untouched

        gate.set()
        rep = await t1
        assert rep.ok and rep.swapped
        assert not await e.svc.vectors.coordinator.is_locked()


async def test_ingest_endpoint_409_while_rebuild_locked(make_app, new_key, tmp_path):
    t, admin = new_key(admin=True)
    (tmp_path / "docs").mkdir()
    async with make_app(ingest_root=str(tmp_path)) as e:
        h = await e.svc.vectors.coordinator.acquire()
        try:
            class Q:
                async def enqueue_job(self, *a):
                    assert a[4] is False, "a rebuild job must not be enqueued while the lock is held"

                    class J:
                        job_id = "j"

                    return J()

            e.svc.arq = Q()
            r = await e.client.post("/ingest", json={"tenant_id": t, "path": "docs", "rebuild": True}, headers=H(admin))
            assert r.status_code == 409 and "Retry-After" in r.headers
            r = await e.client.post("/ingest", json={"tenant_id": t, "path": "docs", "rebuild": False}, headers=H(admin))
            assert r.status_code == 202  # plain ingest is not blocked by the rebuild lock
        finally:
            await e.svc.vectors.coordinator.release(h)


async def test_postgres_advisory_lock_fallback_when_redis_is_down(make_app):
    from app.services.rebuild import RebuildCoordinator

    class Dead:
        async def set(self, *a, **k):
            raise ConnectionError("redis down")

    async with make_app() as e:
        dsn = e.settings.database_url
        c1 = RebuildCoordinator(Dead(), f"pgfb{uuid.uuid4().hex[:6]}", dsn, 5)
        h = await c1.acquire()
        assert h.via == "postgres"
        c2 = RebuildCoordinator(Dead(), c1.alias, dsn, 5)
        with pytest.raises(RebuildInProgress):
            await c2.acquire()
        await c1.release(h)
        h2 = await c2.acquire()  # released with the session
        await c2.release(h2)


# ----------------------------------------------------------------------------- dual-write
async def test_writes_made_during_a_rebuild_survive_the_swap(make_app, tmp_path, monkeypatch):
    ta, tb = f"a{uuid.uuid4().hex[:5]}", f"b{uuid.uuid4().hex[:5]}"
    da = mk(tmp_path, "a", {"a.txt": TEXT})
    db1 = mk(tmp_path, "b1", {"old.txt": "original text about networking and bgp routing. " * 8})
    async with make_app() as e:
        await run_ingest(e, ta, da)
        await run_ingest(e, tb, db1)
        live_v1 = await e.svc.vectors.resolve_alias()
        orig_copy = P._copy_other_tenants

        async def copy_then_concurrent_writes(vs, src, dst, tenant_id, batch):
            n = await orig_copy(vs, src, dst, tenant_id, batch)
            # a normal ingest for ANOTHER tenant lands while the rebuild is in flight (new file + a changed file)
            d2 = mk(tmp_path, "b2", {"old.txt": "REPLACED text about vlan trunking and sdn. " * 8,
                                     "new.txt": "brand new document about ospf areas. " * 8})
            rep = await run_ingest(e, tb, d2)
            assert rep.ok and rep.collection == live_v1  # it wrote to the LIVE collection ...
            return n

        monkeypatch.setattr(P, "_copy_other_tenants", copy_then_concurrent_writes)
        rep = await run_ingest(e, ta, da, rebuild=True)
        assert rep.ok and rep.swapped
        new_live = await e.svc.vectors.resolve_alias()
        assert new_live != live_v1
        texts = " ".join(p.payload["text"] for p in await points_of(e, new_live, tb))
        assert "ospf areas" in texts  # ... and the write survived the swap (mirrored to the candidate)
        assert "REPLACED text" in texts and "original text about networking" not in texts  # stale-point delete mirrored too


# ----------------------------------------------------------------------------- crash recovery
async def test_dead_holder_expires_live_untouched_and_orphan_is_gced(make_app, tmp_path):
    ta = f"a{uuid.uuid4().hex[:5]}"
    d = mk(tmp_path, "a", {"a.txt": TEXT})
    async with make_app(rebuild_lock_ttl_s=3) as e:
        await run_ingest(e, ta, d)
        vs = e.svc.vectors
        live = await vs.resolve_alias()
        before = len(await points_of(e, live, ta))
        # a rebuild process that dies mid-flight: lock + state + half-built candidate, heartbeat gone
        h = await vs.coordinator.acquire()
        h.heartbeat.cancel()
        orphan = await vs.create_versioned(2)
        await vs.coordinator.set_state(h, live, orphan)
        await vs.client.upsert(orphan, [models.PointStruct(
            id=str(uuid.uuid4()), vector={"dense": [0.0] * e.settings.embedding_dim, "sparse": models.SparseVector(indices=[1], values=[1.0])},
            payload={"tenant_id": "ghost", "marker": "orphan", "text": "x", "content_hash": "h"})], wait=True)
        assert await vs.coordinator.is_locked() and await vs.coordinator.mirror_target(live) == orphan
        await asyncio.sleep(3.6)  # no heartbeat => TTL expiry
        assert not await vs.coordinator.is_locked() and await vs.coordinator.mirror_target(live) is None
        assert await vs.resolve_alias() == live and len(await points_of(e, live, ta)) == before  # live never touched
        rep = await run_ingest(e, ta, d, rebuild=True)  # next rebuild GCs the orphan and succeeds
        assert rep.ok and rep.swapped
        new_live = await vs.resolve_alias()
        assert new_live == f"{vs.alias}_v2"
        assert not [p for p in await points_of(e, new_live, "ghost")]  # orphan's marker data is gone (collection was recreated)


async def test_exception_mid_rebuild_cleans_up_and_keeps_live(make_app, tmp_path, monkeypatch):
    ta = f"a{uuid.uuid4().hex[:5]}"
    d = mk(tmp_path, "a", {"a.txt": TEXT})
    async with make_app() as e:
        await run_ingest(e, ta, d)
        vs = e.svc.vectors
        live = await vs.resolve_alias()

        async def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(P, "_copy_other_tenants", boom)
        with pytest.raises(RuntimeError):
            await run_ingest(e, ta, d, rebuild=True)
        assert await vs.resolve_alias() == live and await vs.versions() == [1]  # candidate dropped
        assert not await vs.coordinator.is_locked() and await vs.coordinator.mirror_target(live) is None


# ----------------------------------------------------------------------------- reconciliation
async def test_reconcile_repairs_injected_drift(make_app, tmp_path):
    ta, tb, tc = (f"{c}{uuid.uuid4().hex[:5]}" for c in "abc")
    def multi(w):  # several chunks per document
        return "\n\n".join(f"paragraph {i} " + (w + " ") * 70 for i in range(6))

    async with make_app(chunk_target_tokens=100) as e:
        for t, txt in ((tb, multi("bgp routing peers")), (tc, multi("vlan trunking stp"))):
            await run_ingest(e, t, mk(tmp_path, t, {"x.txt": txt}))
        vs = e.svc.vectors
        live = await vs.resolve_alias()
        cand = await vs.create_versioned(2)
        await P._copy_other_tenants(vs, live, cand, ta, 100)
        assert await _scan_hashes(vs, live, ta, 100) == await _scan_hashes(vs, cand, ta, 100)
        # inject drift into the candidate: a missing point, a point with a wrong hash, and a bogus extra point
        cpts = await points_of(e, cand, tb)
        await vs.client.delete(cand, points_selector=models.PointIdsList(points=[cpts[0].id]), wait=True)
        await vs.client.set_payload(cand, payload={"content_hash": "tampered"}, points=[cpts[1].id] if len(cpts) > 1 else [cpts[0].id], wait=True)
        await vs.client.upsert(cand, [models.PointStruct(
            id=str(uuid.uuid4()), vector={"dense": [0.1] * e.settings.embedding_dim, "sparse": models.SparseVector(indices=[2], values=[1.0])},
            payload={"tenant_id": tc, "content_hash": "bogus", "text": "stale ghost"})], wait=True)
        assert await _scan_hashes(vs, live, ta, 100) != await _scan_hashes(vs, cand, ta, 100)
        stats = await reconcile(vs, live, cand, ta, 100)
        assert stats["copied"] >= 1 and stats["removed"] == 1
        assert await _scan_hashes(vs, live, ta, 100) == await _scan_hashes(vs, cand, ta, 100)


async def test_rebuild_reconciles_drift_before_the_swap(make_app, tmp_path, monkeypatch):
    ta, tb = f"a{uuid.uuid4().hex[:5]}", f"b{uuid.uuid4().hex[:5]}"
    async with make_app() as e:
        await run_ingest(e, ta, mk(tmp_path, "a", {"a.txt": TEXT}))
        await run_ingest(e, tb, mk(tmp_path, "b", {"b.txt": "ospf areas and lsa types. " * 12}))
        vs = e.svc.vectors
        live = await vs.resolve_alias()
        want = await _scan_hashes(vs, live, ta, 100)
        orig = P._copy_other_tenants

        async def copy_then_drift(vs_, src, dst, tenant_id, batch):
            n = await orig(vs_, src, dst, tenant_id, batch)
            ids = [p.id for p in await points_of(e, dst, tb)]
            await vs_.client.delete(dst, points_selector=models.PointIdsList(points=ids), wait=True)  # candidate loses tenant B entirely
            return n

        monkeypatch.setattr(P, "_copy_other_tenants", copy_then_drift)
        rep = await run_ingest(e, ta, mk(tmp_path, "a2", {"a.txt": TEXT}), rebuild=True)
        assert rep.ok and rep.swapped and rep.reconciled["copied"] >= 1
        new_live = await vs.resolve_alias()
        assert await _scan_hashes(vs, new_live, ta, 100) == want  # tenant B is fully back after the swap
