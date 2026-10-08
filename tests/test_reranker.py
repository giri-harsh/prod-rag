"""O-3: the reranker runs in a bounded pool, with limited ONNX threads and truncated passages (real model)."""
import asyncio
import threading
import time

import pytest

from app.services.reranker import FlashRankReranker


@pytest.fixture(scope="module")
def rr(tmp_path_factory):
    try:
        r = FlashRankReranker(cache_dir=str(tmp_path_factory.mktemp("fr")), workers=2, intra_threads=1, max_length=64, timeout_s=0.5)
    except Exception as e:
        pytest.skip(f"FlashRank model unavailable: {e}")
    yield r
    r.close()


def test_onnx_threads_are_configured_not_all_cores(rr):
    so = rr._ranker.session.get_session_options()
    assert so.intra_op_num_threads == 1 and so.inter_op_num_threads == 1


async def test_passages_are_truncated_but_original_text_is_returned(rr):
    seen = {}
    orig = rr._ranker.rerank

    def spy(req):
        seen["lens"] = [len(p["text"]) for p in req.passages]
        return orig(req)

    rr._ranker.rerank = spy
    try:
        big = "autoscaler pods " * 5000  # 80k chars
        out = await rr.rerank("pod autoscaler", [big, "short relevant autoscaler text"], 2)
    finally:
        rr._ranker.rerank = orig
    assert max(seen["lens"]) <= rr.max_length * 4  # cut before tokenising
    assert big in [t for t, _ in out]  # callers still get the full chunk text


async def test_scoring_never_blocks_the_event_loop_and_concurrency_is_bounded(rr, monkeypatch):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow_score(query, docs, top_n):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.15)
        with lock:
            active["now"] -= 1
        return [(d, 0.9) for d in docs[:top_n]]

    monkeypatch.setattr(rr, "_score", slow_score)
    monkeypatch.setattr(rr, "timeout_s", 5.0)  # the timeout covers queue wait too, so give the queued jobs room
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    t0 = time.perf_counter()
    res = await asyncio.gather(*[rr.rerank("q", ["a", "b"], 1) for _ in range(8)])
    wall = time.perf_counter() - t0
    t.cancel()
    assert all(r == [("a", 0.9)] for r in res)
    assert active["max"] <= rr.workers  # the pool bounds concurrent rerank batches
    assert ticks > 20  # the loop kept running while 8 batches were scored (8 x 0.15 s / 2 workers = 0.6 s)
    assert wall >= 0.5  # 8 jobs through 2 workers are queued, not run all at once


async def test_timeout_falls_back_to_fused_order(rr, monkeypatch):
    monkeypatch.setattr(rr, "_score", lambda q, d, n: time.sleep(1.2) or [])  # longer than timeout_s=0.5
    out = await rr.rerank("q", ["x", "y", "z"], 2)
    assert out == [("x", 1.0), ("y", 1.0)]
