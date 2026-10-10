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

    def __init__(self, value=0.75, fail_times=0, exc=None):
        self.value, self.fail_times, self.exc = value, fail_times, exc
        self.calls = 0

    async def abatch_score(self, batch):
        self.calls += 1
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
