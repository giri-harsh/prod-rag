# FlashRank reranking

Stage two of retrieval: Qdrant returns up to `FUSED_K` (10) candidates from the RRF-fused dense+BM25 search; a cross-encoder (FlashRank, ONNX, `ms-marco-TinyBERT-L-2-v2`, CPU) scores each (query, passage) pair and the best `FINAL_K` (5) go to the LLM.
A cross-encoder reads query and passage together, so it separates "mentions the words" from "answers the question" better than vector distance.

## How it is run (`app/services/reranker.py`)
* Loaded **once at startup** (model baked into the image under `FLASHRANK_CACHE`, default `/opt/flashrank`).
* Scored in a **dedicated bounded `ThreadPoolExecutor`** (`RERANK_WORKERS`=2), so it never blocks the event loop and the number of concurrent batches is capped per process.
* ONNX Runtime **intra-op threads = `RERANK_THREADS`** (1) - FlashRank's default session would use every core in every worker.
* Passages are cut to `RERANK_MAX_LENGTH` (512) tokens, pre-truncated by characters before tokenising; callers still receive the full original chunk text.
* `RERANK_TIMEOUT_S` (10) covers queue wait + scoring; on timeout or error the fused order is used with score 1.0 (so the grade/rewrite loop does not spin) and the error is logged.

## Scores drive grading
`rerank()` returns `(text, score)`. In measurement the scores are almost binary: a relevant chunk scored 0.999, irrelevant ones ~0.000, and an off-topic question scored 0.0 on every chunk (`tests/test_retrieval.py::test_real_flashrank_scores_*`).
`RERANK_MIN_SCORE` (0.05) therefore separates "found something" from "nothing relevant"; below it the graph rewrites the query (once) and then answers with the fixed no-context message. Tune it against your own corpus.

## Cost
Measured with ONNX using all cores: ~210 ms to rerank 20 chunks of ~3,600 characters (about 50 ms for 4 short passages), ~3 s one-off model load. Under load the reranker is the dominant CPU consumer on cache misses; see [12 Capacity](12_CAPACITY.md) for the K=20 vs K=10 comparison.
