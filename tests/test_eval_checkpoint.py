"""Phase-2 resume: the checkpoint must save judge calls without ever reporting a stale score.

A full RAGAS run is >1 h of cooldowns against Groq, and one network blip used to discard every metric already
paid for. These tests cover the two ways that fix could go wrong: not resuming (wasted calls) and resuming when
it must not (reporting the previous pipeline's scores as the new one's).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from evals import metrics as M


class FakeScore:
    def __init__(self, value: float):
        self.value = value


class FakeMetric:
    """Stands in for a ragas metric. Counts calls so a resumed metric can be proven not to have run."""

    def __init__(self, value=0.75, fail_times=0, exc=None, fail_on_call=None):
        self.value, self.fail_times, self.exc = value, fail_times, exc
        self.fail_on_call = fail_on_call  # raise on exactly this call number (1-based), to cut a metric in half
        self.calls = 0
        self.seen: list = []

    async def abatch_score(self, batch):
        self.calls += 1
        self.seen.extend(batch)
        if self.fail_on_call == self.calls:
            raise (self.exc or ConnectionError("Connection error."))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise (self.exc or ConnectionError("Connection error."))
        return [FakeScore(self.value) for _ in batch]


def samples(answer="the HPA uses a target utilisation"):
    return [
        {"question": f"q{i}", "actual_response": answer, "actual_contexts": [f"ctx{i}"],
         "reference": f"ref{i}", "expected_tools": ["vector_search"], "actual_tools_called": ["vector_search"]}
        for i in range(3)
    ]


@pytest.fixture
def fake_run(monkeypatch):
    """run_all_metrics with no judge, no embeddings, no cooldowns - just the orchestration under test."""
    monkeypatch.setattr(M, "_build_judge", lambda: (None, None))

    async def no_cooldown(*_a, **_kw):
        return None

    monkeypatch.setattr(M, "_cooldown", no_cooldown)

    made: dict[str, FakeMetric] = {}

    def experiments(_llm, _emb):
        specs = []
        for key, label in (("faithfulness", "Faithfulness"), ("answer_relevancy", "Answer Relevancy")):
            metric = made.setdefault(key, FakeMetric())
            specs.append((key, label, (lambda m=metric: m), lambda s: {"user_input": s["question"]}))
        return specs

    monkeypatch.setattr(M, "_experiments", experiments)
    return made


def run(data, **kw):
    return asyncio.run(M.run_all_metrics(data, **kw))


def test_metrics_are_checkpointed_and_reused(tmp_path, fake_run):
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}

    first = run(data, checkpoint=ckpt)
    assert set(first) == {"faithfulness", "answer_relevancy", "tool_correctness"}
    assert ckpt.exists()
    calls_after_first = {k: m.calls for k, m in fake_run.items()}
    assert all(n > 0 for n in calls_after_first.values())

    prov: dict = {}
    second = run(data, checkpoint=ckpt, provenance=prov)
    # Not one extra judge call, and the same numbers.
    assert {k: m.calls for k, m in fake_run.items()} == calls_after_first
    assert sorted(prov["resumed_metrics"]) == ["answer_relevancy", "faithfulness"]
    for key in ("faithfulness", "answer_relevancy"):
        assert second[key][key].tolist() == first[key][key].tolist()


def test_checkpoint_is_discarded_when_the_answers_change(tmp_path, fake_run):
    """Negative control: an improvement iteration asks the same questions and must be re-scored."""
    ckpt = tmp_path / "phase2.json"
    run({"rag_samples": samples(answer="first answer")}, checkpoint=ckpt)
    before = {k: m.calls for k, m in fake_run.items()}

    prov: dict = {}
    run({"rag_samples": samples(answer="a DIFFERENT answer after a prompt change")},
        checkpoint=ckpt, provenance=prov)

    assert prov["resumed_metrics"] == []
    assert all(m.calls > before[k] for k, m in fake_run.items())


def test_checkpoint_is_ignored_for_a_different_judge_model(tmp_path, fake_run, monkeypatch):
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}
    run(data, checkpoint=ckpt)

    monkeypatch.setattr(M, "JUDGE_MODEL", "some/other-judge")
    prov: dict = {}
    run(data, checkpoint=ckpt, provenance=prov)
    assert prov["resumed_metrics"] == []


def test_corrupt_checkpoint_does_not_abort_the_run(tmp_path, fake_run):
    ckpt = tmp_path / "phase2.json"
    ckpt.write_text("{not json", encoding="utf-8")
    out = run({"rag_samples": samples()}, checkpoint=ckpt)
    assert "faithfulness" in out
    assert json.loads(ckpt.read_text(encoding="utf-8"))["metrics"]["faithfulness"]["rows"]


def test_per_metric_judge_calls_are_recorded(tmp_path, fake_run):
    ckpt = tmp_path / "phase2.json"
    counter = {"n": 0}

    def calls_so_far():
        counter["n"] += 7  # pretend each metric spent 7 calls
        return counter["n"]

    prov: dict = {}
    run({"rag_samples": samples()}, checkpoint=ckpt, calls_so_far=calls_so_far, provenance=prov)
    assert prov["calls_by_metric"] == {"faithfulness": 7, "answer_relevancy": 7}


def test_partial_checkpoint_resumes_only_the_missing_metric(tmp_path, fake_run):
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}
    run(data, checkpoint=ckpt)

    # Drop one metric, as a crash between the two would leave it.
    on_disk = json.loads(ckpt.read_text(encoding="utf-8"))
    del on_disk["metrics"]["answer_relevancy"]
    ckpt.write_text(json.dumps(on_disk), encoding="utf-8")
    before = {k: m.calls for k, m in fake_run.items()}

    prov: dict = {}
    run(data, checkpoint=ckpt, provenance=prov)
    assert prov["resumed_metrics"] == ["faithfulness"]
    assert fake_run["faithfulness"].calls == before["faithfulness"]
    assert fake_run["answer_relevancy"].calls > before["answer_relevancy"]


def test_no_checkpoint_path_still_works(fake_run):
    out = run({"rag_samples": samples()})
    assert round(float(out["faithfulness"]["faithfulness"].mean()), 3) == 0.75


# ── resuming INSIDE a metric ──────────────────────────────────────────────────────────────────────────
# Per-metric granularity was not enough. The judge's hard limit is tokens per DAY, and one metric over 14
# samples is a large part of that budget, so the real run kept dying partway through Faithfulness - which
# checkpointed nothing, because the metric never finished. Three separate attempts scored zero metrics while
# spending ~200,000 judge tokens. Samples are therefore saved as they are scored.

def test_a_metric_cut_off_midway_keeps_the_samples_it_paid_for(tmp_path, fake_run):
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}
    # Dies on the 3rd of 3 samples, the way a daily token cap lands mid-metric.
    fake_run["faithfulness"] = FakeMetric(value=0.5, fail_on_call=3, exc=ValueError("model_not_found"))

    with pytest.raises(ValueError):
        run(data, checkpoint=ckpt)

    on_disk = json.loads(ckpt.read_text(encoding="utf-8"))["metrics"]["faithfulness"]
    assert len(on_disk["rows"]) == 2, "the two scored samples were paid for and must survive"
    assert not on_disk.get("complete")


def test_resuming_scores_only_the_samples_that_were_never_scored(tmp_path, fake_run):
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}
    fake_run["faithfulness"] = FakeMetric(value=0.5, fail_on_call=3, exc=ValueError("model_not_found"))
    with pytest.raises(ValueError):
        run(data, checkpoint=ckpt)

    fake_run["faithfulness"] = FakeMetric(value=0.9)  # a fresh judge for the resumed run
    prov: dict = {}
    out = run(data, checkpoint=ckpt, provenance=prov)

    assert fake_run["faithfulness"].calls == 1, "only the one unscored sample may be re-bought"
    assert [b["user_input"] for b in fake_run["faithfulness"].seen] == ["q2"]
    # The resumed metric still reports all three samples, in the original sample order, mixing the
    # already-paid-for scores with the new one.
    assert out["faithfulness"]["faithfulness"].tolist() == [0.5, 0.5, 0.9]
    assert out["faithfulness"]["question"].tolist() == ["q0", "q1", "q2"]
    assert prov["partial_metrics_resumed"] == {"faithfulness": 2}


def test_a_half_scored_metric_is_not_reported_as_finished(tmp_path, fake_run):
    """Negative control: a partial metric must never be read back as a complete result.

    Its mean over 2 of 14 samples is not the metric, and reporting it as one would put a number in
    evals/baseline_<date>.json that no reader could tell apart from a real measurement.
    """
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}
    fake_run["faithfulness"] = FakeMetric(value=0.5, fail_on_call=3, exc=ValueError("model_not_found"))
    with pytest.raises(ValueError):
        run(data, checkpoint=ckpt)

    fake_run["faithfulness"] = FakeMetric(value=0.9)
    prov: dict = {}
    run(data, checkpoint=ckpt, provenance=prov)
    assert "faithfulness" not in prov["resumed_metrics"], "a partial metric is resumed, not reused wholesale"


def test_partial_rows_are_dropped_when_they_do_not_match_the_samples(tmp_path, fake_run):
    """The rows are a prefix of the sample list, so they are only reusable if the questions still line up."""
    ckpt = tmp_path / "phase2.json"
    data = {"rag_samples": samples()}
    fake_run["faithfulness"] = FakeMetric(value=0.5, fail_on_call=3, exc=ValueError("model_not_found"))
    with pytest.raises(ValueError):
        run(data, checkpoint=ckpt)

    on_disk = json.loads(ckpt.read_text(encoding="utf-8"))
    on_disk["metrics"]["faithfulness"]["rows"][0]["question"] = "a question from another dataset"
    ckpt.write_text(json.dumps(on_disk), encoding="utf-8")

    fake_run["faithfulness"] = FakeMetric(value=0.9)
    out = run(data, checkpoint=ckpt)
    assert fake_run["faithfulness"].calls == 3, "mismatched partial rows must be re-scored, not trusted"
    assert out["faithfulness"]["faithfulness"].tolist() == [0.9, 0.9, 0.9]


# ── riding out the judge's daily token cap ────────────────────────────────────────────────────────────
# Groq's TPD is a leaky bucket that refills continuously (~139 tokens/min for a 200,000/day model), and its
# 429 says exactly how long to wait. Treating that as a failure threw away a whole run three times; it is
# backpressure, so it is waited out rather than retried-then-abandoned.

TPD_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model `qwen/qwen3.8-27b` in "
    "organization `org_x` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 198958, "
    "Requested 3440. Please try again in 17m15.936s. Need more tokens? Upgrade to Dev Tier today', "
    "'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)
TPM_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model `qwen/qwen3.8-27b` in "
    "organization `org_x` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7900, "
    "Requested 300. Please try again in 1.5s.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)


@pytest.mark.parametrize("message,expected", [
    (TPD_429, 17 * 60 + 15.936),
    (TPM_429, 1.5),
    ("Rate limit reached ... Please try again in 8m37.535999999s.", 8 * 60 + 37.535999999),
    ("Rate limit reached ... Please try again in 42s.", 42.0),
])
def test_rate_limit_wait_is_read_from_the_provider_hint(message, expected):
    assert M._rate_limit_wait_s(ValueError(message)) == pytest.approx(expected, abs=0.01)


def test_rate_limit_without_a_hint_falls_back_to_a_default_wait():
    wait = M._rate_limit_wait_s(ValueError("Error code: 429 - rate_limit_exceeded, no hint here"))
    assert wait == M.RATE_LIMIT_DEFAULT_WAIT_S


@pytest.mark.parametrize("exc", [
    ConnectionError("Connection error."),
    ValueError("model_not_found: llama-3.1-8b-instant has been decommissioned"),
    RuntimeError("judge-call budget exceeded (401 > 400)"),
])
def test_rate_limit_negative_control(exc):
    """Only a real rate limit may trigger a 17-minute sleep; everything else must surface now."""
    assert M._rate_limit_wait_s(exc) is None


def test_rate_limit_is_seen_through_the_cause_chain():
    try:
        try:
            raise ValueError(TPD_429)
        except ValueError as cause:
            raise WrappedConnectionError("retries exhausted") from cause
    except WrappedConnectionError as exc:
        assert M._rate_limit_wait_s(exc) == pytest.approx(17 * 60 + 15.936, abs=0.01)


def test_score_batch_waits_out_a_rate_limit_and_then_succeeds(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    metric = FakeMetric(value=0.9, fail_times=1, exc=ValueError(TPD_429))
    scores = asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    assert [s.value for s in scores] == [0.9]
    assert sum(slept) == pytest.approx(17 * 60 + 15.936 + M.RATE_LIMIT_WAIT_MARGIN_S, abs=0.5)


def test_waiting_out_a_rate_limit_does_not_spend_the_transient_retry_budget(monkeypatch):
    """Backpressure is not a failure: a run must not be killed by N rate limits in a row."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    metric = FakeMetric(value=0.9, fail_times=M.TRANSIENT_ATTEMPTS + 3, exc=ValueError(TPM_429))
    scores = asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    assert [s.value for s in scores] == [0.9]
    assert metric.calls == M.TRANSIENT_ATTEMPTS + 4


def test_rate_limit_waiting_is_bounded_and_names_the_knob(monkeypatch):
    """A cap that will not refill in time must stop the run, not sleep until morning."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(M, "RATE_LIMIT_WAIT_BUDGET_S", 1800)
    metric = FakeMetric(fail_times=10_000, exc=ValueError(TPD_429))
    with pytest.raises(RuntimeError, match="JUDGE_RATE_LIMIT_WAIT_BUDGET_S"):
        asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    # 1800s of budget against ~1036s waits: two sleeps, then it gives up instead of a third.
    assert metric.calls == 2


def test_a_rate_limited_metric_still_checkpoints_the_samples_it_finished(tmp_path, fake_run, monkeypatch):
    """The whole point: the daily cap lands mid-metric, and what was already paid for survives it."""
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(M, "RATE_LIMIT_WAIT_BUDGET_S", 0)  # no budget left to wait with
    ckpt = tmp_path / "phase2.json"
    fake_run["faithfulness"] = FakeMetric(value=0.6, fail_on_call=3, exc=ValueError(TPD_429))

    with pytest.raises(RuntimeError, match="JUDGE_RATE_LIMIT_WAIT_BUDGET_S"):
        run({"rag_samples": samples()}, checkpoint=ckpt)

    rows = json.loads(ckpt.read_text(encoding="utf-8"))["metrics"]["faithfulness"]["rows"]
    assert [r["faithfulness"] for r in rows] == [0.6, 0.6]


# ── transient-failure classification ──────────────────────────────────────────────────────────────────

class WrappedConnectionError(Exception):
    """Like instructor.InstructorRetryException: the real cause is only in the __cause__ chain."""


def test_is_transient_sees_through_the_cause_chain():
    import socket

    try:
        try:
            raise socket.gaierror(11001, "getaddrinfo failed")
        except socket.gaierror as cause:
            raise WrappedConnectionError("retries exhausted") from cause
    except WrappedConnectionError as exc:
        assert M._is_transient(exc)


def test_is_transient_matches_the_instructor_message_without_a_cause():
    assert M._is_transient(WrappedConnectionError("Connection error."))


@pytest.mark.parametrize("exc", [
    ValueError("model_not_found: llama-3.1-8b-instant has been decommissioned"),
    KeyError("reference"),
    RuntimeError("judge-call budget exceeded (401 > 400)"),
])
def test_is_transient_rejects_real_failures(exc):
    """Negative control: a retired model or a blown budget must fail fast, not be retried for 3 minutes."""
    assert not M._is_transient(exc)


def test_score_batch_retries_a_transient_failure(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    metric = FakeMetric(value=0.9, fail_times=2)
    scores = asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    assert [s.value for s in scores] == [0.9]
    assert metric.calls == 3
    assert slept == [M.TRANSIENT_BACKOFF_S, 2 * M.TRANSIENT_BACKOFF_S]


def test_score_batch_gives_up_after_the_attempt_limit(monkeypatch):
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    metric = FakeMetric(fail_times=99)
    with pytest.raises(ConnectionError):
        asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    assert metric.calls == M.TRANSIENT_ATTEMPTS


def test_score_batch_does_not_retry_a_permanent_failure(monkeypatch):
    metric = FakeMetric(fail_times=99, exc=ValueError("model_not_found"))
    with pytest.raises(ValueError):
        asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    assert metric.calls == 1


# ── a judge answer cut off by the completion cap ──────────────────────────────────────────────────────

class IncompleteOutputException(Exception):
    """Same name as instructor's, which is how _truncated_output recognises it without importing instructor."""


def test_truncated_output_is_recognised_by_type_and_by_message():
    assert M._truncated_output(IncompleteOutputException("whatever"))
    assert M._truncated_output(ValueError("The output is incomplete due to a max_tokens length limit."))


def test_truncated_output_is_recognised_through_the_cause_chain():
    try:
        try:
            raise IncompleteOutputException("cut off")
        except IncompleteOutputException as cause:
            raise WrappedConnectionError("retries exhausted") from cause
    except WrappedConnectionError as exc:
        assert M._truncated_output(exc)


@pytest.mark.parametrize("exc", [
    ConnectionError("Connection error."),
    ValueError("model_not_found"),
    RuntimeError("judge-call budget exceeded"),
])
def test_truncated_output_negative_control(exc):
    assert not M._truncated_output(exc)


def test_score_batch_fails_fast_on_a_truncated_judge_answer_and_names_the_knob(monkeypatch):
    """Retrying a max_tokens truncation just buys the same cut-off answer three more times.

    The real run died here with a traceback deep in ragas that never mentioned max_tokens, after phase 1 had
    already been paid for; the error now says which setting to change.
    """
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    metric = FakeMetric(fail_times=99, exc=IncompleteOutputException("max_tokens length limit"))
    with pytest.raises(RuntimeError, match="JUDGE_MAX_TOKENS"):
        asyncio.run(M._score_batch(metric, [{"user_input": "q"}], "Faithfulness", None))
    assert metric.calls == 1, "a truncated answer must not be retried"
    assert slept == []
