"""
Phase 2 — RAGAS + Tool Correctness metrics.
Uses JUDGE_GROQ key so production GROQ_API_KEY is never exhausted by eval runs.
All LLM-based metrics run in batches of 5 with 30s cooldowns between sub-batches
and 60s cooldowns between experiments — calibrated for Groq's 6,000 TPM on_demand tier.
Contexts are truncated to 300 chars (2 chunks max) so no single request exceeds the limit.
"""


import os
import asyncio
import datetime as dt
import hashlib
import json
import pathlib
import socket
import sys
import types

import logfire
import pandas as pd
from openai import AsyncOpenAI

# ragas 0.4.3 imports `langchain_community.chat_models.vertexai.ChatVertexAI` unconditionally, and
# langchain-community >= 0.4 removed that module. Pinning langchain-community back to 0.3.x would drag
# langchain-core below 1.0 and break the app itself, so the one symbol ragas needs (for an isinstance check on
# a provider this project never uses) is supplied here instead. Drop this when ragas stops importing it.
if "langchain_community.chat_models.vertexai" not in sys.modules:
    try:
        import langchain_community.chat_models.vertexai  # noqa: F401
    except ImportError:
        _shim = types.ModuleType("langchain_community.chat_models.vertexai")
        _shim.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules["langchain_community.chat_models.vertexai"] = _shim

from ragas.llms import llm_factory
from ragas.embeddings import HuggingFaceEmbeddings
from ragas import SingleTurnSample
from ragas.metrics.collections import (
    Faithfulness,
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    AnswerCorrectness,
)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Groq retired the llama-3.x line this judge used to run on (the key's model list is now gpt-oss, qwen, allam and
# the speech models), so the judge model is read from the environment with a current default. It must stay
# DIFFERENT from the responder model (openai/gpt-oss-120b): a model grading its own family rates itself well.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "qwen/qwen3.8-27b")
COOLDOWN_STANDARD = 62
COOLDOWN_MINI = 40       # between individual samples — lets sliding TPM window recover (~2,800 tok/sample)
GENERAL_BATCH_SIZE = 1  # one sample at a time: abatch_score fires calls concurrently per sample,
                         # so batch>1 stacks multiple samples' async calls inside the same second
CONTEXT_TRUNCATE = 300  # chars per context chunk — reduces single request from ~7,700 to ~400 tokens
CONTEXT_LIMIT = 2       # number of context chunks passed to RAGAS per sample

# Transient-failure handling. A full run is >1 h of wall clock (40 s cooldowns per sample), long enough that a
# laptop's network WILL blip; that must not throw away metrics already paid for.
TRANSIENT_ATTEMPTS = 4          # 1 try + 3 retries per batch
TRANSIENT_BACKOFF_S = 30        # 30s, 60s, 90s
TRANSIENT_EXC_NAMES = {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
                       "ReadTimeout", "RemoteProtocolError", "InternalServerError"}
TRANSIENT_MARKERS = ("Connection error", "getaddrinfo failed", "Temporary failure in name resolution",
                     "Connection reset", "Server disconnected")


def _build_judge():
    api_key = os.getenv("JUDGE_GROQ") or os.getenv("GROQ_API_KEY")
    client = AsyncOpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    llm = llm_factory(JUDGE_MODEL, provider="openai", client=client)
    embeddings = HuggingFaceEmbeddings(
        model="sentence-transformers/all-MiniLM-L6-v2",
        use_api=False,
    )
    return llm, embeddings

async def _cooldown(seconds: int, label: str, status_cb=None):
    msg = f"⏳ {seconds}s cooldown after {label} (Groq TPM buffer)..."
    if status_cb:
        status_cb(msg)
    for _ in range(seconds // 10):
        await asyncio.sleep(10)
    if status_cb:
        status_cb(f"✅ Ready — starting next experiment.")
        
        
def _prep_samples(golden_dataset: dict) -> list:
    """
    Returns only samples with actual_response populated.
    Truncates contexts to CONTEXT_TRUNCATE chars and limits to CONTEXT_LIMIT chunks
    so a single RAGAS LLM call stays well under the 6,000 TPM ceiling.
    (Live contexts from Qdrant are ~1,500 chars each — without truncation a single
    Faithfulness request exceeds 7,000 tokens which hard-fails on the on_demand tier.)
    """
    valid = []
    for s in golden_dataset["rag_samples"]:
        response = s.get("actual_response", "").strip()
        if not response:
            continue
        raw_contexts = s.get("actual_contexts") or s.get("relevant_contexts") or []
        contexts = [c[:CONTEXT_TRUNCATE] for c in raw_contexts[:CONTEXT_LIMIT]]
        valid.append({**s, "actual_contexts": contexts})
    return valid


def _score_df(metric_key: str, samples: list, scores) -> pd.DataFrame:
    return pd.DataFrame([
        {"question": s["question"][:65], metric_key: round(float(r.value), 3)}
        for s, r in zip(samples, scores)
    ])


def _is_transient(exc: BaseException) -> bool:
    """True for a network/upstream blip, as opposed to a bad request or a retired model.

    ragas calls the judge through instructor, which re-raises transport failures as
    InstructorRetryException("Connection error."), so the original socket error is only reachable through the
    __cause__ chain - and sometimes not at all. Both are checked.
    """
    seen, exc_i = set(), exc
    while exc_i is not None and id(exc_i) not in seen:
        seen.add(id(exc_i))
        if isinstance(exc_i, (socket.gaierror, ConnectionError, TimeoutError, asyncio.TimeoutError)):
            return True
        if type(exc_i).__name__ in TRANSIENT_EXC_NAMES:
            return True
        if any(marker in str(exc_i) for marker in TRANSIENT_MARKERS):
            return True
        exc_i = exc_i.__cause__ or exc_i.__context__
    return False


async def _score_batch(metric, batch: list, label: str, status_cb=None) -> list:
    """One abatch_score call, retried through transient failures.

    A 6-metric run takes well over an hour of wall clock, so a laptop whose network drops for a minute used to
    discard every metric already scored (and the judge calls they cost). Transient failures now back off and
    retry the batch; anything else still propagates.
    """
    for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
        try:
            return await metric.abatch_score(batch)
        except Exception as exc:  # noqa: BLE001 - classified by _is_transient, re-raised when not transient
            if attempt == TRANSIENT_ATTEMPTS or not _is_transient(exc):
                raise
            wait = TRANSIENT_BACKOFF_S * attempt
            if status_cb:
                status_cb(f"⚠️ {label}: transient judge failure ({type(exc).__name__}), "
                          f"retry {attempt}/{TRANSIENT_ATTEMPTS - 1} in {wait}s")
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


async def _batched_score(metric, inputs: list, samples: list, status_cb=None, label: str = "") -> list:
    """
    Runs abatch_score in chunks of GENERAL_BATCH_SIZE with cooldowns between chunks.
    Keeps each burst under 6,000 TPM on Groq's on_demand tier.
    """
    all_scores = []
    batches = [inputs[i : i + GENERAL_BATCH_SIZE] for i in range(0, len(inputs), GENERAL_BATCH_SIZE)]
    for b_idx, batch in enumerate(batches):
        if b_idx > 0:
            await _cooldown(COOLDOWN_MINI, f"{label} batch {b_idx}", status_cb)
        scores = await _score_batch(metric, batch, label, status_cb)
        all_scores.extend(scores)
    return all_scores

def _scored_digest(samples: list) -> str:
    """A digest of exactly what the judge is shown, so a checkpoint cannot outlive the run that produced it."""
    h = hashlib.sha256()
    for s in samples:
        h.update(json.dumps([s.get("question"), s.get("actual_response"), s.get("actual_contexts"),
                             s.get("reference")], sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


class Checkpoint:
    """Per-metric scores on disk, so an interrupted phase 2 resumes instead of re-buying every judge call.

    A full 6-metric run is over an hour of wall clock. When a network blip killed one mid-run, four completed
    metrics (and the judge calls they cost) were lost. Each metric is now written as soon as it finishes, keyed
    by a fingerprint of the judge model and of what was scored - question, answer AND contexts. The answers have
    to be in there: an improvement iteration asks the same 15 questions and gets different answers, and reusing
    the old faithfulness scores for them would report the previous pipeline's number as the new one's.
    """

    def __init__(self, path, samples: list, status_cb=None):
        self.path = pathlib.Path(path) if path else None
        self.fingerprint = {"judge_model": JUDGE_MODEL, "n": len(samples),
                            "scored": _scored_digest(samples)}
        self.data: dict = {"fingerprint": self.fingerprint, "metrics": {}}
        self.resumed: list[str] = []
        if not self.path or not self.path.exists():
            return
        try:
            on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if status_cb:
                status_cb(f"checkpoint {self.path} unreadable ({type(exc).__name__}); starting fresh")
            return
        if on_disk.get("fingerprint") != self.fingerprint:
            if status_cb:
                status_cb(f"checkpoint {self.path} is for a different dataset/judge; starting fresh")
            return
        self.data = on_disk
        self.data.setdefault("metrics", {})

    def done(self, key: str):
        """The checkpointed DataFrame for `key`, or None if it was never scored."""
        entry = self.data["metrics"].get(key)
        if not entry or not entry.get("rows"):
            return None
        self.resumed.append(key)
        return pd.DataFrame(entry["rows"])

    def save(self, key: str, df: pd.DataFrame, calls) -> None:
        self.data["metrics"][key] = {"rows": df.to_dict(orient="records"), "calls": calls,
                                     "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic: a crash mid-write must not corrupt the only copy

    @property
    def calls_by_metric(self) -> dict:
        return {k: v.get("calls") for k, v in self.data["metrics"].items()}


def _experiments(judge_llm, ragas_embeddings) -> list:
    """(result key, label, metric factory, sample -> metric input) for the five LLM-judged metrics.

    A factory rather than an instance: a metric is only built when it is about to run, so a resumed run does not
    construct judges it will never use.
    """
    return [
        ("faithfulness", "Faithfulness",
         lambda: Faithfulness(llm=judge_llm),
         lambda s: {"user_input": s["question"], "response": s["actual_response"],
                    "retrieved_contexts": s["actual_contexts"]}),
        ("answer_relevancy", "Answer Relevancy",
         lambda: AnswerRelevancy(llm=judge_llm, embeddings=ragas_embeddings),
         lambda s: {"user_input": s["question"], "response": s["actual_response"]}),
        ("context_precision", "Context Precision",
         lambda: ContextPrecision(llm=judge_llm),
         lambda s: {"user_input": s["question"], "reference": s["reference"],
                    "retrieved_contexts": s["actual_contexts"]}),
        ("context_recall", "Context Recall",
         lambda: ContextRecall(llm=judge_llm),
         lambda s: {"user_input": s["question"], "reference": s["reference"],
                    "retrieved_contexts": s["actual_contexts"]}),
        ("answer_correctness", "Answer Correctness",
         lambda: AnswerCorrectness(llm=judge_llm, embeddings=ragas_embeddings),
         lambda s: {"user_input": s["question"], "response": s["actual_response"],
                    "reference": s["reference"]}),
    ]


def _tool_correctness(samples: list) -> pd.DataFrame:
    """Jaccard overlap of called vs expected tools. No LLM, so it is never checkpointed."""
    rows = []
    for s in samples:
        called = set(s.get("actual_tools_called") or [])
        expected = set(s.get("expected_tools") or [])
        union = len(called | expected)
        rows.append({"question": s["question"][:65],
                     "tool_correctness": round(len(called & expected) / union if union else 0.0, 3)})
    return pd.DataFrame(rows)


async def run_all_metrics(golden_dataset: dict, status_cb=None, checkpoint=None, calls_so_far=None,
                          provenance: dict | None = None) -> dict:
    """
    Runs all 6 experiments. Returns dict keyed by metric name -> DataFrame.
    status_cb(message: str) is called for live UI updates.

    checkpoint    - path to a resume file; metrics already in it are reused instead of re-scored.
    calls_so_far  - callable returning the judge-call count so far, used to record what each metric cost.
    provenance    - dict filled in with which metrics were resumed and what each one cost.
    """
    judge_llm, ragas_embeddings = _build_judge()
    samples = _prep_samples(golden_dataset)

    if not samples:
        raise ValueError("No samples with actual_response found. Run Phase 1 first.")

    results = {}
    ckpt = Checkpoint(checkpoint, samples, status_cb)
    experiments = _experiments(judge_llm, ragas_embeddings)
    total = len(experiments) + 1
    pending_cooldown = None  # label of the last metric that actually called the judge

    with logfire.span("Eval Phase 2 - all metrics", total_samples=len(samples)):
        for i, (key, label, build_metric, make_input) in enumerate(experiments, start=1):
            cached = ckpt.done(key)
            if cached is not None:
                results[key] = cached
                if status_cb:
                    status_cb(f"Exp {i}/{total} - {label}: reusing {len(cached)} checkpointed scores")
                continue

            # The cooldown belongs BEFORE the next judged metric, not after the previous one: a resumed run must
            # not sit through a TPM cooldown for a metric it read off disk, and the last metric needs none.
            if pending_cooldown:
                await _cooldown(COOLDOWN_STANDARD, pending_cooldown, status_cb)
            if status_cb:
                status_cb(f"Exp {i}/{total} - {label} ({len(samples)} samples)...")
            with logfire.span(f"Exp {i} - {label}"):
                before = calls_so_far() if calls_so_far else None
                inputs = [make_input(s) for s in samples]
                scores = await _batched_score(build_metric(), inputs, samples, status_cb, label)
                df = _score_df(key, samples, scores)
                results[key] = df
                spent = (calls_so_far() - before) if calls_so_far else None
                ckpt.save(key, df, spent)
                logfire.info(f"{label} done", avg=round(df[key].mean(), 3), judge_calls=spent)
            pending_cooldown = label

        if status_cb:
            status_cb(f"Exp {total}/{total} - Tool Correctness (zero LLM calls)...")
        with logfire.span(f"Exp {total} - Tool Correctness"):
            df = _tool_correctness(samples)
            results["tool_correctness"] = df
            logfire.info("Tool Correctness done", avg=round(df["tool_correctness"].mean(), 3))

        if status_cb:
            status_cb(f"All {total} experiments complete!")

    if provenance is not None:
        provenance["resumed_metrics"] = list(ckpt.resumed)
        provenance["calls_by_metric"] = ckpt.calls_by_metric
        provenance["checkpoint"] = str(ckpt.path) if ckpt.path else None

    return results
