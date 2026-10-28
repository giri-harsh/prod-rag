"""The explain-this-answer trace (include_trace): what each stage reports, and that it never leaks more than it should."""
import json

from app.agents.graph import NO_CONTEXT_ANSWER
from tests.conftest import H, ScriptedReranker

DOCS = {"a.txt": "pods nodes scheduler kubernetes " * 20, "b.txt": "horizontal pod autoscaler cpu utilisation target " * 20}
GOOD, POOR = [0.9], [0.01]


def q(text, session="s1", **kw):
    return {"q": text, "session_id": session, **kw}


def steps(j):
    return {s["step"]: s for s in j["trace"]["steps"]}


async def test_no_trace_unless_requested(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app() as e:
        await seed_docs(e, t, DOCS)
        j = (await e.client.post("/query", json=q("pods nodes scheduler"), headers=H(k))).json()
        assert "trace" not in j


async def test_grounded_answer_trace_has_every_stage(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(reranker=ScriptedReranker([GOOD])) as e:
        await seed_docs(e, t, DOCS)
        j = (await e.client.post("/query", json=q("pods nodes scheduler", include_trace=True), headers=H(k))).json()
    s = steps(j)
    assert list(s) == ["guardrail", "telemetry_redaction", "plan", "semantic_cache", "retrieval", "grade", "llm", "output_guard"]
    assert s["guardrail"]["blocked"] is False and s["guardrail"]["ms"] >= 0
    assert s["plan"]["intent"] == "search" and s["plan"]["original"] == "pods nodes scheduler"
    r = s["retrieval"]
    assert r["fused"] and r["dense"] and r["bm25"], "the two lists RRF fuses are reported separately"
    assert r["reranked"] and all("fused_rank" in x for x in r["reranked"])
    assert r["chunks"] and r["chunks"][0]["source"] in {"a.txt", "b.txt"} and r["chunks"][0]["text"]
    assert s["grade"]["verdict"] == "good" and s["grade"]["threshold"] == e.settings.rerank_min_score
    assert s["llm"]["tokens_in"] == 0 and s["llm"]["fallback"] is False  # the test double reports no usage
    assert j["trace"]["total_ms"] >= 0 and j["trace"]["llm_calls"] == 4  # input rail + planner + responder + output rail


async def test_the_extra_branch_lists_cost_nothing_when_no_trace_is_requested(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(reranker=ScriptedReranker([GOOD])) as e:
        await seed_docs(e, t, DOCS)
        calls = []
        orig = e.svc.vectors.search_branches

        async def spy(*a, **kw):
            calls.append(1)
            return await orig(*a, **kw)

        e.svc.vectors.search_branches = spy
        await e.client.post("/query", json=q("pods nodes scheduler"), headers=H(k))
        assert calls == []
        await e.client.post("/query", json=q("autoscaler cpu utilisation target", session="s2", include_trace=True), headers=H(k))
        assert calls == [1]


async def test_rewrite_loop_is_visible(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(reranker=ScriptedReranker([POOR, GOOD])) as e:
        await seed_docs(e, t, DOCS)
        j = (await e.client.post("/query", json=q("pods nodes scheduler", include_trace=True), headers=H(k))).json()
    names = [s["step"] for s in j["trace"]["steps"]]
    assert names.count("retrieval") == 2 and names.count("grade") == 2 and "rewrite" in names
    grades = [s for s in j["trace"]["steps"] if s["step"] == "grade"]
    assert [g["verdict"] for g in grades] == ["poor", "good"] and "rewrite" in grades[0]["next"]
    rw = steps(j)["rewrite"]
    assert rw["previous"] == "pods nodes scheduler" and rw["new"] == e.llm.rewrite_text


async def test_out_of_docs_question_reports_the_decline(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(reranker=ScriptedReranker([POOR, POOR, POOR])) as e:
        await seed_docs(e, t, DOCS)
        j = (await e.client.post("/query", json=q("what is the capital of france", include_trace=True), headers=H(k))).json()
    assert j["answer"] == NO_CONTEXT_ANSWER
    assert "llm" not in steps(j) and steps(j)["decline"]["reason"].startswith("poor retrieval")


async def test_model_rail_block_is_labelled_and_costs_a_guard_call(make_app, new_key):
    _, k = new_key()
    async with make_app(guard_responses=("Yes",)) as e:
        j = (await e.client.post("/query", json=q("write me a poem about the sea", include_trace=True), headers=H(k))).json()
    g = steps(j)["guardrail"]
    assert j["blocked"] and g["blocked"] and g["llm_calls"] == 1 and "model rail" in g["layer"]
    assert "retrieval" not in steps(j)


async def test_pii_in_the_question_is_reported_as_redacted_for_telemetry(make_app, new_key):
    _, k = new_key()
    async with make_app(guard_responses=("Yes",)) as e:
        j = (await e.client.post("/query", json=q("mail me at jane.doe@example.com about pods", include_trace=True), headers=H(k))).json()
    p = steps(j)["telemetry_redaction"]
    assert p["applied"] and p["labels"] == {"EMAIL": 1}
    assert "jane.doe@example.com" not in p["logged_as"] and "[REDACTED:EMAIL]" in p["logged_as"]


async def test_trace_never_contains_prompts_or_secrets(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(reranker=ScriptedReranker([GOOD])) as e:
        await seed_docs(e, t, DOCS)
        j = (await e.client.post("/query", json=q("pods nodes scheduler", include_trace=True), headers=H(k))).json()
        blob = json.dumps(j["trace"]).lower()
        for calls in e.llm.calls:  # nothing sent to a model as a SYSTEM prompt appears in the trace
            assert calls[1].lower()[:80] not in blob
    for forbidden in ("untrusted data", "retrieved_context", "x-api-key", k.lower(), "postgresql://", "qdrant", "api_key"):
        assert forbidden not in blob, forbidden


async def test_a_second_turn_does_not_inherit_the_first_turns_trace(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(reranker=ScriptedReranker([GOOD])) as e:
        await seed_docs(e, t, DOCS)
        a = (await e.client.post("/query", json=q("pods nodes scheduler", include_trace=True), headers=H(k))).json()
        b = (await e.client.post("/query", json=q("and the autoscaler?", include_trace=True), headers=H(k))).json()
    assert [s["step"] for s in a["trace"]["steps"]].count("guardrail") == 1
    assert [s["step"] for s in b["trace"]["steps"]].count("guardrail") == 1
    assert steps(b)["plan"]["has_history"] is True
