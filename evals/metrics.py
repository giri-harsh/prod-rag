"""
Phase 2 — RAGAS + Tool Correctness metrics.
Uses JUDGE_GROQ key so production GROQ_API_KEY is never exhausted by eval runs.
All LLM-based metrics run one sample at a time with cooldowns between samples and between experiments —
calibrated for the 8,000 TPM / 200,000 TPD the judge model gets on Groq's on_demand tier.
The judge is shown exactly the contexts the responder was shown (see _prep_samples).
"""


import os
import asyncio
import datetime as dt
import hashlib
import json
import pathlib
import re
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

from app.guardrails.injection import wrap_untrusted

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Groq retired the llama-3.x line this judge used to run on (the key's model list is now gpt-oss, qwen, allam and
# the speech models), so the judge model is read from the environment with a current default. It must stay
# DIFFERENT from the responder model (openai/gpt-oss-120b): a model grading its own family rates itself well.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "qwen/qwen3.8-27b")
# qwen3 bills its thinking trace as completion tokens: ~2,500 tokens a judgment, so the judge model's
# 200,000 tokens/day buys about 60 judgments - less than one metric over 14 samples. "none" turns thinking off
# and is recorded in the result file and in the checkpoint fingerprint, because it is a judge-quality tradeoff,
# not an implementation detail. Set JUDGE_REASONING=default to let the provider decide again.
JUDGE_REASONING = os.getenv("JUDGE_REASONING", "none")
# ragas defaults its judge to max_tokens=1024 (InstructorModelArgs), and faithfulness asks for ONE VERDICT PER
# STATEMENT with a written reason - a 10-statement answer does not fit, so instructor raises
# IncompleteOutputException("output is incomplete due to a max_tokens length limit") and the whole run dies after
# phase 1 has already been paid for. This is a cap, not a spend: the judge is billed for what it emits.
JUDGE_MAX_TOKENS = int(os.getenv("JUDGE_MAX_TOKENS", "8192"))
COOLDOWN_STANDARD = 62
COOLDOWN_MINI = 40       # between individual samples — lets sliding TPM window recover
GENERAL_BATCH_SIZE = 1  # one sample at a time: abatch_score fires calls concurrently per sample,
                         # so batch>1 stacks multiple samples' async calls inside the same second
# What the judge is allowed to see, in chars. This is the RESPONDER's context budget (Settings.max_context_chars),
# not a judge-side economy measure: see _prep_samples.
JUDGE_CONTEXT_CHARS = int(os.getenv("JUDGE_CONTEXT_CHARS", "25000"))
_CTX_BLOCK = re.compile(r'<retrieved_context id="\d+">\n(.*?)\n</retrieved_context>', re.S)

# Transient-failure handling. A full run is >1 h of wall clock (40 s cooldowns per sample), long enough that a
# laptop's network WILL blip; that must not throw away metrics already paid for.
TRANSIENT_ATTEMPTS = 4          # 1 try + 3 retries per batch
TRANSIENT_BACKOFF_S = 30        # 30s, 60s, 90s
TRANSIENT_EXC_NAMES = {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
                       "ReadTimeout", "RemoteProtocolError", "InternalServerError"}
TRANSIENT_MARKERS = ("Connection error", "getaddrinfo failed", "Temporary failure in name resolution",
                     "Connection reset", "Server disconnected")

# Rate limits are backpressure, not failure. Groq's tokens-per-day cap is a leaky bucket that refills continuously
# and its 429 says how long to wait ("Please try again in 17m15.936s"), so the judge waits that long (plus a
# margin) instead of dying. The wait is bounded per judge call: a cap that will not refill inside the budget
# stops the run with a message naming the knob, rather than sleeping until morning.
RATE_LIMIT_DEFAULT_WAIT_S = 60.0     # when a 429 carries no "try again in" hint
RATE_LIMIT_WAIT_MARGIN_S = 5.0       # added to the provider's hint (it is rounded down)
RATE_LIMIT_WAIT_BUDGET_S = float(os.getenv("JUDGE_RATE_LIMIT_WAIT_BUDGET_S", "7200"))
_RATE_LIMIT_MARKERS = ("429", "rate_limit_exceeded", "Rate limit reached")
_RETRY_HINT = re.compile(r"try again in\s+(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m(?!s))?\s*(?:(\d+(?:\.\d+)?)s)?", re.I)


def _apply_reasoning_policy(client):
    """Send reasoning_effort on every judge request, if JUDGE_REASONING asks for one.

    ragas builds its own request bodies, so the only place to inject a provider parameter is the client. It goes
    through extra_body rather than a named kwarg so an SDK that does not know the parameter still passes it on.
    """
    if JUDGE_REASONING in ("", "default", "provider"):
        return client
    inner = client.chat.completions.create  # already wrapped by the caller's judge-call counter, if there is one

    async def create(*a, **kw):
        kw["extra_body"] = {**(kw.get("extra_body") or {}), "reasoning_effort": JUDGE_REASONING}
        return await inner(*a, **kw)

    client.chat.completions.create = create
    return client


def _build_judge():
    api_key = os.getenv("JUDGE_GROQ") or os.getenv("GROQ_API_KEY")
    client = _apply_reasoning_policy(AsyncOpenAI(api_key=api_key, base_url=GROQ_BASE_URL))
    llm = llm_factory(JUDGE_MODEL, provider="openai", client=client, max_tokens=JUDGE_MAX_TOKENS)
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
        
        
def _contexts_the_responder_saw(raw_contexts: list) -> list:
    """The chunk texts that actually reached the responder's prompt, in its order, untruncated.

    Faithfulness asks "is every statement in the answer supported by the context?", so it is only a measurement
    of the pipeline if the judge sees the SAME context the answer was written from. This used to pass 2 chunks cut
    to 300 chars each - 600 of ~7,500 chars - to stay under a TPM ceiling, and the result was a faithfulness of
    0.235 that said nothing about the responder: grounded answers like "a Job belongs to the batch/v1 API group"
    scored 0.0 because the sentence supporting them sat past the cut.

    The selection is delegated to the production helper (whole chunks until the budget runs out, tag-like text
    stripped) instead of being re-implemented here, so the two cannot drift apart.
    """
    return _CTX_BLOCK.findall(wrap_untrusted(list(raw_contexts), JUDGE_CONTEXT_CHARS))


def _prep_samples(golden_dataset: dict) -> list:
    """Samples with an answer, each carrying exactly the context its answer was written from."""
    valid = []
    for s in golden_dataset["rag_samples"]:
        response = s.get("actual_response", "").strip()
        if not response:
            continue
        raw_contexts = list(s.get("actual_contexts") or s.get("relevant_contexts") or [])
        valid.append({**s, "actual_contexts": _contexts_the_responder_saw(raw_contexts),
                      "actual_contexts_raw": raw_contexts})
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


def _rate_limit_wait_s(exc: BaseException) -> float | None:
    """Seconds the provider asked us to wait if `exc` (or anything in its cause chain) is a 429, else None.

    Only a real rate limit may trigger a long sleep; every other failure must surface immediately.
    """
    seen, exc_i = set(), exc
    while exc_i is not None and id(exc_i) not in seen:
        seen.add(id(exc_i))
        text = str(exc_i)
        if any(marker in text for marker in _RATE_LIMIT_MARKERS):
            m = _RETRY_HINT.search(text)
            if m and any(m.groups()):
                h, mins, s = (float(g) if g else 0.0 for g in m.groups())
                return h * 3600 + mins * 60 + s
            return RATE_LIMIT_DEFAULT_WAIT_S
        exc_i = exc_i.__cause__ or exc_i.__context__
    return None


def _truncated_output(exc: BaseException) -> bool:
    """True when the judge's structured answer was cut off by the completion cap.

    instructor raises IncompleteOutputException for this. It is worth naming, because the traceback it produces
    points at ragas internals and says nothing about the one knob that fixes it - which cost a full phase-1 run
    to work out once already.
    """
    seen, exc_i = set(), exc
    while exc_i is not None and id(exc_i) not in seen:
        seen.add(id(exc_i))
        if type(exc_i).__name__ == "IncompleteOutputException" or "max_tokens length limit" in str(exc_i):
            return True
        exc_i = exc_i.__cause__ or exc_i.__context__
    return False


async def _score_batch(metric, batch: list, label: str, status_cb=None) -> list:
    """One abatch_score call, retried through transient failures.

    A 6-metric run takes well over an hour of wall clock, so a laptop whose network drops for a minute used to
    discard every metric already scored (and the judge calls they cost). Transient failures now back off and
    retry the batch; anything else still propagates. A 429 is waited out for as long as the provider says
    (bounded by RATE_LIMIT_WAIT_BUDGET_S) and does not spend the transient-retry attempts.
    """
    attempt, rate_waited = 0, 0.0
    while True:
        attempt += 1
        try:
            return await metric.abatch_score(batch)
        except Exception as exc:  # noqa: BLE001 - classified by _is_transient, re-raised when not transient
            if _truncated_output(exc):
                raise RuntimeError(
                    f"{label}: the judge's structured output was cut off at max_tokens="
                    f"{JUDGE_MAX_TOKENS}. Retrying cannot help - raise JUDGE_MAX_TOKENS and re-run; the "
                    f"metrics already checkpointed are kept.") from exc
            limit_wait = _rate_limit_wait_s(exc)
            if limit_wait is not None:
                wait = limit_wait + RATE_LIMIT_WAIT_MARGIN_S
                if rate_waited + wait > RATE_LIMIT_WAIT_BUDGET_S:
                    raise RuntimeError(
                        f"{label}: the judge is rate-limited and the next wait ({wait:.0f}s) would exceed the "
                        f"{RATE_LIMIT_WAIT_BUDGET_S:.0f}s budget already {rate_waited:.0f}s used. Raise "
                        f"JUDGE_RATE_LIMIT_WAIT_BUDGET_S or re-run later; the samples already scored are "
                        f"checkpointed.") from exc
                rate_waited += wait
                attempt -= 1  # backpressure is not a failure: it must not use up the transient retries
                if status_cb:
                    status_cb(f"⏳ {label}: judge rate limit, waiting {wait:.0f}s for the bucket to refill")
                await asyncio.sleep(wait)
                continue
            if attempt == TRANSIENT_ATTEMPTS or not _is_transient(exc):
                raise
            wait = TRANSIENT_BACKOFF_S * attempt
            if status_cb:
                status_cb(f"⚠️ {label}: transient judge failure ({type(exc).__name__}), "
                          f"retry {attempt}/{TRANSIENT_ATTEMPTS - 1} in {wait}s")
            await asyncio.sleep(wait)


async def _batched_score(metric, inputs: list, samples: list, status_cb=None, label: str = "",
                         on_progress=None) -> list:
    """
    Runs abatch_score in chunks of GENERAL_BATCH_SIZE with cooldowns between chunks.
    Keeps each burst under 6,000 TPM on Groq's on_demand tier.
    on_progress(scores_so_far) is called after every chunk so the caller can persist what was paid for.
    """
    all_scores = []
    batches = [inputs[i : i + GENERAL_BATCH_SIZE] for i in range(0, len(inputs), GENERAL_BATCH_SIZE)]
    for b_idx, batch in enumerate(batches):
        if b_idx > 0:
            await _cooldown(COOLDOWN_MINI, f"{label} batch {b_idx}", status_cb)
        scores = await _score_batch(metric, batch, label, status_cb)
        all_scores.extend(scores)
        if on_progress:
            on_progress(list(all_scores))
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
        self.fingerprint = {"judge_model": JUDGE_MODEL, "judge_reasoning": JUDGE_REASONING, "n": len(samples),
                            "scored": _scored_digest(samples)}
        self.data: dict = {"fingerprint": self.fingerprint, "metrics": {}}
        self.resumed: list[str] = []
        self.partial_resumed: dict[str, int] = {}
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
        # An entry without a "complete" flag predates partial checkpoints and was only ever written finished.
        if not entry or not entry.get("rows") or entry.get("complete") is False:
            return None
        self.resumed.append(key)
        return pd.DataFrame(entry["rows"])

    def partial(self, key: str, samples: list) -> list[dict]:
        """Rows already scored for a metric that was cut off midway, or [] if none can be trusted.

        The rows are a prefix of the sample list (samples are scored in order), so they are only reusable while
        every row still names the question at its position; any mismatch discards all of them.
        """
        entry = self.data["metrics"].get(key)
        if not entry or entry.get("complete") is not False:
            return []
        rows = entry.get("rows") or []
        if not rows or len(rows) >= len(samples):
            return []
        if any(r.get("question") != s["question"][:65] or key not in r for r, s in zip(rows, samples)):
            return []
        self.partial_resumed[key] = len(rows)
        return rows

    def save_partial(self, key: str, rows: list[dict]) -> None:
        """Persist the samples scored so far; not a finished metric, so done() never returns it."""
        self._write(key, rows, None, None, complete=False)

    def save(self, key: str, df: pd.DataFrame, calls, tokens=None) -> None:
        self._write(key, df.to_dict(orient="records"), calls, tokens, complete=True)

    def _write(self, key: str, rows: list[dict], calls, tokens, complete: bool) -> None:
        self.data["metrics"][key] = {"rows": rows, "complete": complete, "calls": calls, "tokens": tokens,
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

    @property
    def tokens_by_metric(self) -> dict:
        return {k: v.get("tokens") for k, v in self.data["metrics"].items()}


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
                          provenance: dict | None = None, tokens_so_far=None) -> dict:
    """
    Runs all 6 experiments. Returns dict keyed by metric name -> DataFrame.
    status_cb(message: str) is called for live UI updates.

    checkpoint    - path to a resume file; metrics already in it are reused instead of re-scored.
    calls_so_far  - callable returning the judge-call count so far, used to record what each metric cost.
    tokens_so_far - callable returning judge tokens so far; the judge's hard limit is tokens per day, not calls.
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
                before_tok = tokens_so_far() if tokens_so_far else None
                prior = ckpt.partial(key, samples)  # samples a previous, cut-off run already paid for
                if prior and status_cb:
                    status_cb(f"{label}: resuming after {len(prior)}/{len(samples)} checkpointed samples")
                todo = samples[len(prior):]
                inputs = [make_input(s) for s in todo]

                def keep(new_scores, key=key, prior=prior, todo=todo):
                    ckpt.save_partial(key, prior + _score_df(key, todo, new_scores).to_dict(orient="records"))

                scores = await _batched_score(build_metric(), inputs, todo, status_cb, label, on_progress=keep)
                df = _score_df(key, todo, scores)
                if prior:
                    df = pd.concat([pd.DataFrame(prior), df], ignore_index=True)
                results[key] = df
                spent = (calls_so_far() - before) if calls_so_far else None
                spent_tok = (tokens_so_far() - before_tok) if tokens_so_far else None
                ckpt.save(key, df, spent, spent_tok)
                logfire.info(f"{label} done", avg=round(df[key].mean(), 3), judge_calls=spent,
                             judge_tokens=spent_tok)
                if status_cb:
                    status_cb(f"{label}: mean {round(float(df[key].mean()), 3)} "
                              f"({spent} calls, {spent_tok} tokens)")
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
        provenance["partial_metrics_resumed"] = dict(ckpt.partial_resumed)
        provenance["calls_by_metric"] = ckpt.calls_by_metric
        provenance["tokens_by_metric"] = ckpt.tokens_by_metric
        provenance["checkpoint"] = str(ckpt.path) if ckpt.path else None
        # What the judge was actually shown, so no reader has to trust that it matched the responder's prompt.
        provenance["judge_context"] = {
            "budget_chars": JUDGE_CONTEXT_CHARS,
            "chunks": [len(s["actual_contexts"]) for s in samples],
            "chars": [sum(len(c) for c in s["actual_contexts"]) for s in samples],
            "dropped_by_budget": [
                {"question": s["question"][:65],
                 "chunks_retrieved": len(s.get("actual_contexts_raw") or s["actual_contexts"])}
                for s in samples
                if len(s["actual_contexts"]) < len(s.get("actual_contexts_raw") or s["actual_contexts"])],
            "reasoning": JUDGE_REASONING,
            "judge_model": JUDGE_MODEL,
            "max_tokens": JUDGE_MAX_TOKENS,
        }

    return results
