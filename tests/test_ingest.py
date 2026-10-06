"""P1-5 ingestion: determinism/idempotency, batching, blue/green swap, error reporting, worker, /ingest."""
import uuid

import pytest
from qdrant_client import models

from app.errors import AppError
from app.ingestion.pipeline import ingest, point_id
from app.worker import ingest_job
from tests.conftest import H

TEXT = "kubernetes pods are scheduled onto nodes. " * 5


async def count(env, tenant=None):
    flt = None
    if tenant:
        flt = models.Filter(must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant))])
    return (await env.svc.vectors.client.count(env.settings.qdrant_collection, count_filter=flt, exact=True)).count


def test_point_ids_are_deterministic_and_tenant_scoped():
    a = point_id("t1", "f.txt", 0, "hello")
    assert a == point_id("t1", "f.txt", 0, "hello")
    assert a != point_id("t2", "f.txt", 0, "hello")
    assert a != point_id("t1", "f.txt", 1, "hello") and a != point_id("t1", "f.txt", 0, "hellp")


async def test_reingestion_produces_no_duplicates(make_app, seed_docs):
    t = f"t{uuid.uuid4().hex[:6]}"
    async with make_app() as e:
        r1 = await seed_docs(e, t, {"a.txt": TEXT, "b.txt": TEXT + " more"})
        n1 = await count(e, t)
        assert r1.ok and n1 > 0 and r1.chunks_embedded == n1
        e.embedder.calls = 0
        r2 = await seed_docs(e, t, {"a.txt": TEXT, "b.txt": TEXT + " more"})
        assert await count(e, t) == n1 and r2.chunks_embedded == 0 and e.embedder.calls == 0  # nothing re-embedded


async def test_changed_file_replaces_stale_points(make_app, tmp_path):
    t = f"t{uuid.uuid4().hex[:6]}"
    f = tmp_path / "a.txt"
    async with make_app() as e:
        f.write_text("old content about kubernetes " * 3)
        await ingest(e.svc.vectors, e.embedder, e.settings, t, str(f))
        f.write_text("completely new content about networking " * 3)
        await ingest(e.svc.vectors, e.embedder, e.settings, t, str(f))
        pts, _ = await e.svc.vectors.client.scroll(e.settings.qdrant_collection, limit=50, with_payload=True)
        texts = [p.payload["text"] for p in pts if p.payload["tenant_id"] == t]
        assert texts and all("networking" in x for x in texts)


async def test_upserts_are_batched(make_app, tmp_path):
    t = f"t{uuid.uuid4().hex[:6]}"
    f = tmp_path / "big.txt"
    f.write_text("\n\n".join(f"paragraph {i} " + "word " * 280 for i in range(12)))  # many ~1.4k-char chunks
    async with make_app(ingest_batch_size=3) as e:
        sizes = []
        orig = e.embedder.embed_texts

        async def spy(texts):
            sizes.append(len(texts))
            return await orig(texts)

        e.embedder.embed_texts = spy
        rep = await ingest(e.svc.vectors, e.embedder, e.settings, t, str(f))
        assert rep.chunks_total > 3 and max(sizes) <= 3 and len(sizes) > 1


async def test_alias_swap_blue_green_preserves_other_tenants(make_app, seed_docs):
    ta, tb = f"a{uuid.uuid4().hex[:5]}", f"b{uuid.uuid4().hex[:5]}"
    async with make_app(keep_old_collections=1) as e:
        vs = e.svc.vectors
        await seed_docs(e, ta, {"a.txt": TEXT})
        await seed_docs(e, tb, {"b.txt": TEXT + " beta"})
        before = await vs.resolve_alias()
        nb = await count(e, tb)

        rep = await seed_docs(e, ta, {"a.txt": TEXT + " v2"}, rebuild=True)
        after = await vs.resolve_alias()
        assert rep.ok and rep.swapped and after != before and after.endswith("_v2")
        assert await count(e, tb) == nb  # other tenant's data was carried over
        assert (await vs.client.collection_exists(before))  # old version kept for rollback

        await seed_docs(e, ta, {"a.txt": TEXT + " v3"}, rebuild=True)
        assert (await vs.resolve_alias()).endswith("_v3")
        assert not await vs.client.collection_exists(before)  # pruned beyond keep_old_collections
        new_meta = (await vs.client.get_collection(await vs.resolve_alias())).config.metadata
        assert new_meta["embedding_dim"] == e.settings.embedding_dim


async def test_failed_rebuild_does_not_swap_and_reports_errors(make_app, seed_docs):
    t = f"t{uuid.uuid4().hex[:6]}"
    async with make_app() as e:
        await seed_docs(e, t, {"a.txt": TEXT})
        active = await e.svc.vectors.resolve_alias()
        rep = await seed_docs(e, t, {"good.txt": TEXT, "empty.txt": "   "}, rebuild=True)
        assert not rep.ok and rep.failed[0][0] == "empty.txt" and rep.files_ok == 1
        assert not rep.swapped and await e.svc.vectors.resolve_alias() == active
        assert await e.svc.vectors.versions() == [1]  # aborted candidate was cleaned up


async def test_worker_job_raises_on_failures_and_succeeds_otherwise(make_app, tmp_path, settings):
    t = f"t{uuid.uuid4().hex[:6]}"
    async with make_app() as e:
        ctx = {"vs": e.svc.vectors, "embedder": e.embedder, "s": e.settings}
        good = tmp_path / "g"
        good.mkdir()
        (good / "a.txt").write_text(TEXT)
        out = await ingest_job(ctx, t, str(good), None, False)
        assert out["files"] == 1
        bad = tmp_path / "b"
        bad.mkdir()
        (bad / "e.txt").write_text(" ")
        with pytest.raises(AppError):
            await ingest_job(ctx, t, str(bad), None, False)


async def test_ingest_endpoint_enqueues_for_admin_only(make_app, new_key, tmp_path):
    t, admin = new_key(admin=True)
    _, user = new_key()
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    async with make_app(ingest_root=str(root)) as e:
        class Q:
            jobs = []

            async def enqueue_job(self, name, *args):
                Q.jobs.append((name, args))

                class J:
                    job_id = "job-1"

                return J()

        e.svc.arq = Q()
        body = {"tenant_id": t, "path": "docs"}
        assert (await e.client.post("/ingest", json=body, headers=H(user))).status_code == 403
        assert (await e.client.post("/ingest", json=body)).status_code == 401
        r = await e.client.post("/ingest", json=body, headers=H(admin))
        assert r.status_code == 202 and r.json()["job_id"] == "job-1"
        assert Q.jobs[0][0] == "ingest_job" and Q.jobs[0][1][0] == t
        assert (await e.client.post("/ingest", json={"tenant_id": t, "path": "../../etc"}, headers=H(admin))).status_code == 400


async def test_loaders_html_txt_docx_pptx(tmp_path):
    from docx import Document
    from pptx import Presentation

    from app.ingestion.loaders.html import parse_html
    from app.ingestion.loaders.office import parse_office
    from app.ingestion.loaders.text import parse_text

    (tmp_path / "a.html").write_text("<html><script>x()</script><body><p>hello html</p></body></html>")
    (tmp_path / "a.txt").write_text("hello txt")
    d = Document()
    d.add_paragraph("hello docx")
    d.save(tmp_path / "a.docx")
    p = Presentation()
    s = p.slides.add_slide(p.slide_layouts[5])
    s.shapes.title.text = "hello pptx"
    p.save(tmp_path / "a.pptx")
    assert "hello html" in parse_html(str(tmp_path / "a.html")) and "x()" not in parse_html(str(tmp_path / "a.html"))
    assert parse_text(str(tmp_path / "a.txt")) == "hello txt"
    assert "hello docx" in parse_office(str(tmp_path / "a.docx"))
    assert "hello pptx" in parse_office(str(tmp_path / "a.pptx"))
