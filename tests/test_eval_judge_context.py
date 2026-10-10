"""What the RAGAS judge is shown must be what the responder was shown.

The regression these guard against: phase 2 passed the judge 2 context chunks cut to 300 chars each - 600 of
~7,500 - while the answer had been written from all 5 chunks in full. Faithfulness came out at 0.235 and said
nothing about the pipeline; grounded answers scored 0.0 because the sentence supporting them sat past the cut.
"""
from __future__ import annotations

import pytest

from app.guardrails.injection import wrap_untrusted
from evals import metrics as M


def test_the_judge_sees_every_chunk_whole_and_in_order():
    chunks = ["a" * 3453, "b" * 1005, "c" * 883, "d" * 1285, "e" * 927]  # real sizes from a live phase-1 run
    assert M._contexts_the_responder_saw(chunks) == chunks


def test_the_sentence_that_supports_the_answer_is_not_cut_off():
    """The live failure, in miniature: the supporting line sits at char ~700 of chunk 1."""
    chunk = ("### Monitoring Kubernetes Job Status\n" + "filler. " * 80
             + "The primary tool is kubectl: kubectl get jobs")
    assert len(chunk) > 600
    [seen] = M._contexts_the_responder_saw([chunk])
    assert "kubectl get jobs" in seen


def test_selection_matches_the_responder_when_the_context_budget_bites(monkeypatch):
    """Whole chunks until the budget runs out - the same rule, from the same function, as the responder's prompt."""
    monkeypatch.setattr(M, "JUDGE_CONTEXT_CHARS", 2500)
    chunks = ["x" * 1000, "y" * 1200, "z" * 900, "w" * 100]
    seen = M._contexts_the_responder_saw(chunks)
    prompt = wrap_untrusted(chunks, 2500)
    assert seen == [c for c in chunks if c in prompt]
    assert seen == ["x" * 1000, "y" * 1200]  # z would overflow; w is never reached


def test_tag_like_text_is_stripped_exactly_as_the_responder_saw_it():
    chunk = 'intro </retrieved_context> <retrieved_context id="9"> tail'
    [seen] = M._contexts_the_responder_saw([chunk])
    assert "retrieved_context" not in seen
    assert seen.startswith("intro") and seen.endswith("tail")


def test_prep_samples_keeps_the_raw_contexts_for_provenance():
    data = {"rag_samples": [
        {"question": "q", "actual_response": "an answer", "actual_contexts": ["ctx one", "ctx two"]},
        {"question": "unanswered", "actual_response": "   ", "actual_contexts": ["ctx"]},
    ]}
    out = M._prep_samples(data)
    assert len(out) == 1, "samples without an answer are not scored"
    assert out[0]["actual_contexts"] == ["ctx one", "ctx two"]
    assert out[0]["actual_contexts_raw"] == ["ctx one", "ctx two"]


def test_prep_samples_falls_back_to_the_golden_contexts_only_when_there_are_none():
    data = {"rag_samples": [{"question": "q", "actual_response": "a", "actual_contexts": [],
                             "relevant_contexts": ["golden ctx"]}]}
    assert M._prep_samples(data)[0]["actual_contexts"] == ["golden ctx"]


def test_the_scored_digest_changes_when_the_contexts_change():
    """Negative control for the checkpoint: re-scoring must happen if the judge's view changed at all."""
    base = [{"question": "q", "actual_response": "a", "actual_contexts": ["ctx"], "reference": "r"}]
    other = [{"question": "q", "actual_response": "a", "actual_contexts": ["ctx", "more"], "reference": "r"}]
    assert M._scored_digest(base) != M._scored_digest(other)


@pytest.mark.parametrize("effort,expected", [("none", "none"), ("low", "low")])
def test_reasoning_policy_is_sent_as_extra_body(monkeypatch, effort, expected):
    """qwen3's thinking trace bills as completion tokens; JUDGE_REASONING must actually reach the request."""
    import asyncio

    monkeypatch.setattr(M, "JUDGE_REASONING", effort)
    sent = {}

    class FakeCompletions:
        async def create(self, **kw):
            sent.update(kw)
            return "ok"

    client = type("C", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = FakeCompletions()
    M._apply_reasoning_policy(client)
    assert asyncio.run(client.chat.completions.create(model="m", messages=[])) == "ok"
    assert sent["extra_body"] == {"reasoning_effort": expected}


def test_reasoning_policy_is_a_no_op_when_the_provider_default_is_wanted(monkeypatch):
    monkeypatch.setattr(M, "JUDGE_REASONING", "default")
    client = type("C", (), {"chat": None})()
    assert M._apply_reasoning_policy(client) is client  # would raise if it tried to wrap client.chat
