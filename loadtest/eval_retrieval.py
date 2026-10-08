"""Retrieval quality matrix on the labeled set, with REAL Gemini embeddings.

    PYTHONPATH=. python loadtest/eval_retrieval.py [--ks 10 20] [--json out.json]

Measures hit@1, hit@5, MRR and phrase@5 for every combination of
  retrieval mode : dense-only | sparse-only | hybrid (dense + sparse -> server-side RRF)
  reranker       : off (fusion/score order) | on (FlashRank re-orders the SAME candidate set)
  FUSED_K        : how many candidates are fused/reranked before the metrics are computed
so the only thing that differs between two cells is the thing being compared.

Unlike `eval_hit_at_k.py` (fake hash embedder, meant for FUSED_K comparisons under load), this one calls the real
Gemini embedding API. The free tier allows 100 embed REQUESTS PER MINUTE per model and langchain sends one request
per text, so: query embeddings are paced (--rpm), can be cached between runs (--qvec-cache), and the index is
reused when it already exists (--reingest forces a rebuild). One full matrix therefore costs one embedding per
question, not one per cell.

A label is `{"q", "source", "kw"}`: `source` is the file that answers the question, `kw` a phrase that must occur in
the answering chunk. Metrics are computed at FILE level (that is what a citation shows the user); phrase@5 is the
stricter chunk-level check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from qdrant_client import models  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.ingestion.pipeline import ingest  # noqa: E402
from app.resilience import Breakers  # noqa: E402
from app.services.embedding import GeminiEmbedder  # noqa: E402
from app.services.reranker import FlashRankReranker  # noqa: E402
from app.services.sparse import make_sparse_encoder  # noqa: E402
from app.services.vectorstore import VectorStore, make_client  # noqa: E402

HERE = Path(__file__).resolve().parent
TENANT = "retrievaleval"
MODES = ("dense", "sparse", "hybrid")


def metrics(ranked: list[tuple[str, str]], want_source: str, kw: str) -> dict:
    """`ranked` is the final ordering as (text, source).

    File level: hit@1/@3/@5 and the reciprocal rank of the first chunk from the right file.
    Chunk level: phrase@1/@5 - the answering phrase is actually inside a top chunk, which is what the responder
    gets to read. With only six source files, hit@5 saturates; phrase@k and hit@1 are the discriminating numbers.
    """
    srcs = [s for _, s in ranked]
    rr = next((1 / i for i, s in enumerate(srcs, 1) if s == want_source), 0.0)
    hit = lambda k: float(want_source in srcs[:k])  # noqa: E731
    phrase = lambda k: float(any(kw in t.lower() and s == want_source for t, s in ranked[:k]))  # noqa: E731
    return {"hit@1": hit(1), "hit@3": hit(3), "hit@5": hit(5), "mrr": rr, "phrase@1": phrase(1), "phrase@5": phrase(5)}


async def search(vs: VectorStore, mode: str, dense, sparse, k: int, prefetch_k: int):
    """One candidate set. hybrid goes through the production path; dense/sparse query a single vector directly."""
    if mode == "hybrid":
        return await vs.search(TENANT, dense, sparse, prefetch_k, k)
    flt = models.Filter(must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=TENANT))])
    resp = await vs.client.query_points(
        collection_name=vs.alias, query=dense if mode == "dense" else sparse,
        using=mode, query_filter=flt, limit=k, with_payload=True,
    )
    return [p for p in resp.points]


def payload_of(hit):
    """vs.search returns Hit objects, the single-vector path returns ScoredPoints: normalise to (text, source)."""
    if hasattr(hit, "payload"):
        return hit.payload.get("text", ""), hit.payload.get("source", "unknown")
    return hit.text, hit.source


async def main(ks: list[int], json_out: str | None, reingest: bool, rpm: float, qvec_cache: str | None) -> dict:
    labeled = json.loads((HERE / "labeled_set.json").read_text(encoding="utf-8"))
    # a 429 from the free tier asks for a ~60 s wait, so the cap has to allow it (the API default of 15 s would not)
    s = load_settings(qdrant_collection="retrieval_eval", semcache_enabled=False, retry_after_cap_s=75.0, max_attempts=5)
    b = Breakers(s.breaker_failures, s.breaker_reset_s)
    client = make_client(s)
    vs = VectorStore(client, s, b)
    await vs.ensure_active()
    emb, sparse_enc = GeminiEmbedder(s, b), make_sparse_encoder(s.sparse_backend)
    have = 0
    if not reingest:
        pts0, _ = await client.scroll(await vs.resolve_alias(), limit=1, with_payload=False)
        have = len(pts0)
    ingest_s = 0.0
    if reingest or not have:
        t0 = time.perf_counter()
        # not rebuild=True: point ids are deterministic (tenant|source|idx|sha256), so re-ingesting is idempotent
        rep = await ingest(vs, emb, sparse_enc, s, TENANT, str(HERE.parent / "DATA" / "true_data"), "true")
        assert rep.ok, rep.failed
        ingest_s = round(time.perf_counter() - t0, 1)

    # the labels must be answerable from the INDEXED text, not just from the file on disk
    pts, _ = await client.scroll(await vs.resolve_alias(), limit=10000, with_payload=True)
    by_src: dict[str, str] = {}
    for p in pts:
        by_src[p.payload["source"]] = by_src.get(p.payload["source"], "") + "\n" + p.payload["text"].lower()
    bad = [(x["source"], x["kw"]) for x in labeled if x["kw"].lower() not in by_src.get(x["source"], "")]
    assert not bad, f"labels whose phrase is not in the indexed chunks: {bad}"

    rr = FlashRankReranker(os.environ.get("FLASHRANK_CACHE") or tempfile.mkdtemp(), workers=1, intra_threads=1)
    cache_path = Path(qvec_cache) if qvec_cache else None
    qvec: dict[str, list[float]] = {}
    if cache_path and cache_path.exists():
        qvec = {q: v for q, v in json.loads(cache_path.read_text(encoding="utf-8")).items() if len(v) == s.embedding_dim}
    qsp: dict[str, object] = {}
    gap = 60.0 / rpm if rpm > 0 else 0.0
    embedded = 0
    for x in labeled:  # one embedding per question, reused by every cell of the matrix
        qsp.setdefault(x["q"], await sparse_enc.encode_query(x["q"]))
        if x["q"] not in qvec:
            if embedded and gap:
                await asyncio.sleep(gap)  # stay under the provider's requests-per-minute quota
            qvec[x["q"]] = await emb.embed_query(x["q"])
            embedded += 1
    if cache_path and embedded:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(qvec), encoding="utf-8")

    cells: dict[str, dict] = {}
    for mode in MODES:
        for k in ks:
            t0 = time.perf_counter()
            cand = {}
            for x in labeled:
                cand[x["q"]] = [payload_of(h) for h in await search(vs, mode, qvec[x["q"]], qsp[x["q"]], k, s.prefetch_k)]
            search_ms = round(1000 * (time.perf_counter() - t0) / len(labeled), 1)
            for rerank in (False, True):
                acc = dict.fromkeys(("hit@1", "hit@3", "hit@5", "mrr", "phrase@1", "phrase@5"), 0.0)
                t1 = time.perf_counter()
                for x in labeled:
                    pairs = cand[x["q"]]
                    if rerank and pairs:
                        texts = [t for t, _ in pairs]
                        order = await rr.rerank(x["q"], texts, len(pairs))
                        # map back by POSITION, not by text: two chunks can share text and would collide in a dict
                        seen: dict[str, list[int]] = {}
                        for i, t in enumerate(texts):
                            seen.setdefault(t, []).append(i)
                        pairs = [pairs[seen[t].pop(0)] for t, _ in order]
                    m = metrics(pairs, x["source"], x["kw"].lower())
                    for key in acc:
                        acc[key] += m[key]
                cells[f"{mode}|k={k}|rerank={'on' if rerank else 'off'}"] = {
                    **{key: round(100 * v / len(labeled), 1) for key, v in acc.items()},
                    "search_ms": search_ms,
                    "rerank_ms": round(1000 * (time.perf_counter() - t1) / len(labeled), 1) if rerank else 0.0,
                }

    best = max(cells.items(), key=lambda kv: (kv[1]["hit@5"], kv[1]["mrr"], -kv[1]["rerank_ms"]))
    default = f"hybrid|k={s.fused_k}|rerank=on"  # the production path always reranks (app/container.py)
    result = {
        "n_labels": len(labeled),
        "sources": sorted(by_src),
        "chunks_indexed": len(pts),
        "qdrant": s.qdrant_url,
        "ingested_this_run": bool(ingest_s),
        "ingest_seconds": ingest_s,
        "query_embeddings_this_run": embedded,
        "embedding": f"{s.embedding_model}/{s.embedding_dim} (REAL Gemini API)",
        "sparse": s.sparse_backend,
        "reranker": "FlashRank (real)",
        "ks": ks,
        "default_config": default,
        "best_config": best[0],
        "margin_hit@5_best_minus_default": round(best[1]["hit@5"] - cells[default]["hit@5"], 1),
        "cells": cells,
    }
    await client.close()
    rr.close()
    if json_out:
        Path(json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ks", type=int, nargs="+", default=[10, 20])
    ap.add_argument("--json", default=str(HERE / "retrieval_eval_result.json"))
    ap.add_argument("--reingest", action="store_true", help="re-embed and re-index DATA/true_data even if the collection already has points")
    ap.add_argument("--rpm", type=float, default=80, help="max embedding requests per minute (free tier allows 100); 0 disables pacing")
    ap.add_argument("--qvec-cache", help="JSON file of question -> query vector, so a re-run of the matrix costs no embeddings")
    a = ap.parse_args()
    out = asyncio.run(main(a.ks, a.json, a.reingest, a.rpm, a.qvec_cache))
    print(json.dumps({k: v for k, v in out.items() if k != "cells"}, indent=2))
    print("\n| config | hit@1 | hit@3 | hit@5 | MRR | phrase@1 | phrase@5 | search ms | rerank ms |")
    print("|---|---|---|---|---|---|---|---|---|")
    for name, c in out["cells"].items():
        print(f"| {name} | {c['hit@1']} | {c['hit@3']} | {c['hit@5']} | {c['mrr']} | {c['phrase@1']} | "
              f"{c['phrase@5']} | {c['search_ms']} | {c['rerank_ms']} |")
