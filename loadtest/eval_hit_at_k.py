"""Retrieval quality on a labeled set: hit@5 for different FUSED_K values.

  PYTHONPATH=. python loadtest/eval_hit_at_k.py [--ks 20 10]

Pipeline measured: REAL BM25 sparse (fastembed) + FAKE dense (hash bag-of-words embedder from app.loadtest, NOT a semantic model)
-> Qdrant RRF fusion -> REAL FlashRank rerank -> top 5. The dense side being fake means absolute numbers are NOT representative of
production recall; only the K=20 vs K=10 comparison under identical conditions is meaningful.

hit@5      : the expected source file is among the 5 reranked chunks.
phrase@5   : stricter - a chunk containing the expected answer phrase (`kw`) is among the 5 reranked chunks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import Settings  # noqa: E402
from app.ingestion.pipeline import ingest  # noqa: E402
from app.loadtest import LoadEmbedder  # noqa: E402
from app.resilience import Breakers  # noqa: E402
from app.services.reranker import FlashRankReranker  # noqa: E402
from app.services.sparse import make_sparse_encoder  # noqa: E402
from app.services.vectorstore import VectorStore, make_client  # noqa: E402

HERE = Path(__file__).resolve().parent
TENANT = "evalset"


async def main(ks: list[int], data_dir: str, json_out: str | None) -> dict:
    labeled = json.loads((HERE / "labeled_set.json").read_text(encoding="utf-8"))
    s = Settings(_env_file=None, database_url="postgresql://x", redis_url="redis://x", qdrant_url=":memory:",
                 qdrant_collection="evalset", embedding_dim=768, embedding_model="loadtest-embed",
                 loadtest_embed_ms=1, loadtest_embed_jitter_ms=0)
    client = make_client(s)
    vs = VectorStore(client, s, Breakers(5, 30))
    await vs.ensure_active()
    emb, sparse = LoadEmbedder(s), make_sparse_encoder("bm25")
    rep = await ingest(vs, emb, sparse, s, TENANT, data_dir, "true")
    assert rep.ok, rep.failed
    rr = FlashRankReranker(os.environ.get("FLASHRANK_CACHE") or tempfile.mkdtemp(), workers=1, intra_threads=1)
    # validate the labels themselves: the expected phrase must exist in the expected source
    pts, _ = await client.scroll(await vs.resolve_alias(), limit=5000, with_payload=True)
    by_src: dict[str, str] = {}
    for p in pts:
        by_src[p.payload["source"]] = by_src.get(p.payload["source"], "") + "\n" + p.payload["text"].lower()
    bad = [x for x in labeled if x["kw"].lower() not in by_src.get(x["source"], "")]
    assert not bad, f"labels whose phrase is not in the source: {[(b['q'], b['source'], b['kw']) for b in bad]}"
    result: dict = {"n": len(labeled), "dense": "FAKE (hash bag-of-words)", "sparse": "real fastembed BM25", "rerank": "real FlashRank", "by_k": {}}
    for k in ks:
        hit = phrase = 0
        for x in labeled:
            dense = (await emb.embed_query(x["q"]))
            sp = await sparse.encode_query(x["q"])
            hits = await vs.search(TENANT, dense, sp, s.prefetch_k, k)
            top = await rr.rerank(x["q"], [h.text for h in hits], 5)
            src_of = {h.text: h.source for h in hits}
            srcs = [src_of[t] for t, _ in top]
            hit += x["source"] in srcs
            phrase += any(x["kw"].lower() in t.lower() and src_of[t] == x["source"] for t, _ in top)
        result["by_k"][k] = {"hit@5": round(100 * hit / len(labeled), 1), "phrase@5": round(100 * phrase / len(labeled), 1)}
    await client.close()
    rr.close()
    if json_out:
        Path(json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ks", type=int, nargs="+", default=[20, 10])
    ap.add_argument("--data", default=str(HERE.parent / "DATA" / "true_data"))
    ap.add_argument("--json")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(main(a.ks, a.data, a.json)), indent=2))
