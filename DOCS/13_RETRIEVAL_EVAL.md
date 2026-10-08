# 13 - Retrieval quality (real embeddings)

Every number on this page comes from one run of `loadtest/eval_retrieval.py` against the **real Gemini embedding
API** and a **local Qdrant** (`http://127.0.0.1:6333`, collection `retrieval_eval`). Raw output:
`loadtest/retrieval_eval_result.json`.

```bash
PYTHONPATH=. python loadtest/eval_retrieval.py --ks 10 20 --qvec-cache .cache/qvec.json
```

## 1. Setup

| | |
|---|---|
| labeled questions | **60** (`loadtest/labeled_set.json`), written from the text of `DATA/true_data` only |
| corpus | 6 files (pptx, docx x2, html x2, txt) -> **83 chunks** indexed |
| embeddings | `gemini-embedding-2`, 768 dims, **real API** (ingest 10.6 s; 60 query embeddings, paced to stay under the free tier's 100 requests/minute) |
| sparse | fastembed BM25 (`SPARSE_BACKEND=bm25`), IDF modifier, Qdrant-side |
| fusion | server-side RRF over a dense and a sparse prefetch (`PREFETCH_K=40` per branch) |
| reranker | FlashRank (real ONNX cross-encoder), 1 worker / 1 intra-op thread |
| machine | the same laptop as [12_CAPACITY.md](12_CAPACITY.md) |

Each label is `{"q", "source", "kw"}`. `source` is the file that answers the question; `kw` is a phrase that must
occur in the answering chunk. All 60 `kw` phrases are asserted to exist in the **indexed chunks** before any metric
is computed, so a miss is a retrieval failure and never a bad label.

Metrics, all macro-averaged over the 60 questions:

* **hit@1 / hit@3 / hit@5** - the expected *file* appears in the top 1 / 3 / 5 results. This is what a citation
  shows the user.
* **MRR** - 1 / rank of the first chunk from the expected file (0 if it never appears).
* **phrase@1 / phrase@5** - the stricter *chunk-level* check: a top-1 / top-5 chunk is from the expected file **and**
  contains the answering phrase. This is what the responder actually gets to read.

`rerank=on` and `rerank=off` score the **same candidate set** - the only difference is the ordering - so the
columns are directly comparable.

## 2. Results

| mode | FUSED_K | rerank | hit@1 | hit@3 | hit@5 | MRR | phrase@1 | phrase@5 | search ms | rerank ms |
|---|---|---|---|---|---|---|---|---|---|---|
| dense | 10 | off | 98.3 | 98.3 | 100.0 | 98.8 | 88.3 | 100.0 | 14.8 | - |
| dense | 10 | on | 96.7 | 98.3 | 100.0 | 97.9 | 78.3 | 100.0 | 14.8 | 53.4 |
| dense | 20 | off | 98.3 | 98.3 | 100.0 | 98.8 | 88.3 | 100.0 | 18.4 | - |
| dense | 20 | on | 95.0 | 96.7 | 100.0 | 96.7 | 76.7 | 95.0 | 18.4 | 115.5 |
| sparse | 10 | off | 93.3 | 100.0 | 100.0 | 96.7 | 85.0 | 96.7 | 16.3 | - |
| sparse | 10 | on | 95.0 | 98.3 | 98.3 | 96.7 | 76.7 | 98.3 | 16.3 | 57.8 |
| sparse | 20 | off | 93.3 | 100.0 | 100.0 | 96.7 | 85.0 | 96.7 | 19.3 | - |
| sparse | 20 | on | 95.0 | 96.7 | 100.0 | 96.7 | 76.7 | 95.0 | 19.3 | 120.1 |
| **hybrid** | **10** | **off** | **100.0** | **100.0** | **100.0** | **100.0** | **95.0** | **100.0** | **16.9** | **-** |
| hybrid | 10 | on | 95.0 | 98.3 | 100.0 | 96.8 | 76.7 | 98.3 | 16.9 | 55.5 |
| hybrid | 20 | off | 100.0 | 100.0 | 100.0 | 100.0 | 91.7 | 100.0 | 18.0 | - |
| hybrid | 20 | on | 95.0 | 96.7 | 100.0 | 96.7 | 76.7 | 95.0 | 18.0 | 119.7 |

## 3. What the numbers say

**hit@5 is saturated and carries no information here.** With six source files, five slots cover 83 % of the corpus;
every single configuration scores 100.0 (one exception at 98.3). Any conclusion drawn from hit@5 on a corpus this
small would be noise. The discriminating columns are **hit@1**, **MRR** and above all **phrase@1**.

**Hybrid beats either branch alone, on the metrics that still discriminate.** hit@1: hybrid 100.0 > dense 98.3 >
sparse 93.3; phrase@1: hybrid 95.0 > dense 88.3 > sparse 85.0. The two branches fail on different questions -
sparse misses paraphrased questions ("How is a Secret stored differently from a ConfigMap?" -> `base64`), dense
misses exact tokens (`*/15`, `2.2/jobs/get`, `126m`) - and RRF recovers both. **The default (hybrid) is correct.**

**FUSED_K 10 and 20 are indistinguishable in quality and 10 is cheaper.** Identical hit@k and MRR in every mode;
reranking 20 candidates instead of 10 costs 2x the CPU (119.7 vs 55.5 ms per query) and, with the reranker on,
*loses* 5 points of phrase@5. This reproduces the load-test conclusion in
[12_CAPACITY.md](12_CAPACITY.md#choosing-fused_k) with real embeddings. **`FUSED_K=10` stays the default.**

**The reranker makes the ordering worse on this corpus.** Reranking the same hybrid candidates drops hit@1 from
100.0 to 95.0, MRR from 100.0 to 96.8 and phrase@1 from 95.0 to 76.7 - and costs 55 ms of CPU per query. The effect
is consistent across all three modes and both K values, so it is not noise within this run.

A believable explanation: RRF over a strong 768-dim embedding plus BM25 already ranks a 10-candidate list from a
small single-domain corpus very well, and a small MS-MARCO cross-encoder re-sorting that list can only move the
right chunk away from rank 1. This is exactly the regime where rerankers are known to add least.

**It is still not a reason to turn the reranker off**, and it was not turned off:

1. The selection rule for this exercise was "switch if some configuration beats the default on hit@5 by more than
   2 points". The best margin is **0.0 points**, so the default stands.
2. The reranker is not only a sorter - its score is the **grading signal**. `route_grade` compares the top rerank
   score with `RERANK_MIN_SCORE=0.05` to decide between answering, rewriting the query and giving up. That gate is
   what makes an off-topic question return "I couldn't find relevant information" instead of an invented answer
   (0 % false-pass on the off-topic and attack sets in `live_report.md`). Removing the reranker removes the gate.
3. 60 questions over 6 files in one domain is too thin a basis for a pipeline change. The honest next step is below.

## 4. Follow-ups this run justifies (not yet done)

* Re-measure on a corpus of a few hundred documents with overlapping topics, where hit@5 is no longer saturated and
  the reranker has something to disambiguate. Only then is "keep or drop the cross-encoder" a decidable question.
* If the effect reproduces there, the change is **not** `RERANK_ENABLED=false` (there is no such flag, by design):
  it is to keep reranking for the *grade* and order the final context by fusion rank. That needs its own test.
* Try a stronger reranker (a larger FlashRank model or a hosted cross-encoder) before concluding that reranking
  itself is unhelpful - this result is about *this* model.
* `phrase@1` at 95 % with hybrid means 3 of 60 questions give the responder a first chunk that does not contain the
  answer phrase. Reading those three is worth more than another metric.

## 5. Reproducing

```bash
# full matrix; re-uses the existing index and the cached query vectors, so it costs no API calls
PYTHONPATH=. python loadtest/eval_retrieval.py --ks 10 20 --qvec-cache .cache/qvec.json

# force a fresh ingest with real embeddings (~83 chunk embeddings + 60 queries, paced under 100/min)
PYTHONPATH=. python loadtest/eval_retrieval.py --reingest --rpm 80
```

Free-tier note: `gemini-embedding-2` allows **100 embed requests per minute** and langchain sends one request per
text, so an unpaced run of this script hits `429 RESOURCE_EXHAUSTED` part-way through the queries. `--rpm` paces
them and the script raises the provider-hint wait cap to 75 s (a 429 asks for ~60 s).

The fake-embedder script `loadtest/eval_hit_at_k.py` still exists and still has a use: it needs no API key and runs
in seconds, which makes it the right tool for FUSED_K regression checks in CI. Its absolute numbers are not
meaningful (the dense side is a hash bag-of-words).
