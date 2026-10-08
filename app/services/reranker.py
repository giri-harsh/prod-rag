"""FlashRank reranker: loaded once at startup, scored in a BOUNDED thread pool so it can never block the event loop or
oversubscribe the CPU. Returns (text, score) best-first.

* `workers`   - size of the dedicated ThreadPoolExecutor (max concurrent rerank batches per process)
* `intra_threads` - ONNX Runtime intra-op threads per batch (default 1: with N gunicorn workers x M pool threads x all-core ONNX
                    the box would be oversubscribed; total reranker threads per process = workers x intra_threads)
* `max_length` - passages are cut to this many tokens (and pre-truncated by characters so huge chunks are not even tokenised)
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import logfire

Scored = list[tuple[str, float]]
CHARS_PER_TOKEN = 4


class Reranker(Protocol):
    async def rerank(self, query: str, docs: list[str], top_n: int) -> Scored: ...


class FlashRankReranker:
    def __init__(self, cache_dir: str = "/opt/flashrank", workers: int = 2, intra_threads: int = 1,
                 max_length: int = 512, timeout_s: float = 10.0):
        import onnxruntime as ort
        from flashrank import Ranker
        from flashrank.Config import default_model, model_file_map

        self.max_length, self.timeout_s = max_length, timeout_s
        self._ranker = Ranker(cache_dir=cache_dir, max_length=max_length)  # downloads the model on first use
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, intra_threads)
        so.inter_op_num_threads = 1
        model_file = self._ranker.model_dir / model_file_map[default_model]
        self._ranker.session = ort.InferenceSession(str(model_file), sess_options=so)  # replace the all-cores default session
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="rerank")
        self.intra_threads, self.workers = so.intra_op_num_threads, max(1, workers)

    def _score(self, query: str, docs: list[str], top_n: int) -> Scored:
        from flashrank import RerankRequest

        cut = self.max_length * CHARS_PER_TOKEN
        passages = [{"id": i, "text": d[:cut]} for i, d in enumerate(docs)]  # truncate BEFORE tokenising
        res = self._ranker.rerank(RerankRequest(query=query, passages=passages))
        full = dict(enumerate(docs))
        return [(full[r["id"]], float(r["score"])) for r in res[:top_n]]  # return the full original text, not the truncated one

    async def rerank(self, query: str, docs: list[str], top_n: int) -> Scored:
        if not docs:
            return []
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(loop.run_in_executor(self._pool, self._score, query, docs, top_n), self.timeout_s)
        except Exception as e:  # degrade to fused order (score 1.0 so grading does not loop) rather than failing
            logfire.error("rerank failed or timed out; using fused order", error=repr(e))
            return [(d, 1.0) for d in docs[:top_n]]

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class NoopReranker:
    async def rerank(self, query: str, docs: list[str], top_n: int) -> Scored:
        return [(d, 1.0) for d in docs[:top_n]]
