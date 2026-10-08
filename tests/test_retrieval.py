"""R-1 chunking, R-2 hybrid search, R-3 tunables, R-4 tenant domain, C-1/C-2/C-3 cyclic graph, D-7 contexts."""
import os
import re
import uuid

import pytest
from qdrant_client import models

from app.agents.graph import NO_CONTEXT_ANSWER
from app.auth import keys
from app.ingestion.chunking.splitter import est_tokens, split_pages
from app.services.sparse import Bm25SparseEncoder, SimpleSparseEncoder
from tests.conftest import H, ScriptedReranker, make_guard

DOC = (
    "# Cluster Guide\n\nIntro paragraph about clusters.\n\n## Scheduling\n\n"
    + "\n\n".join(f"Scheduling paragraph {i}. " + "pods nodes scheduler " * 60 for i in range(8))
    + "\n\n## Networking\n\n" + "\n\n".join(f"Networking paragraph {i}. " + "bgp vlan routing " * 60 for i in range(4))
)


def q(text, session="s1", **kw):
    return {"q": text, "session_id": session, **kw}


# ------------------------------------------------------------------------- R-1 chunking
def test_chunker_size_overlap_headings_and_pages():
    pages = [(1, DOC[: len(DOC) // 2]), (2, DOC[len(DOC) // 2 :])]
    chunks = split_pages(pages, target_tokens=300, overlap_ratio=0.15, min_tokens=100)
    assert len(chunks) > 3
    assert all(est_tokens(c.text) <= 300 * 1.25 for c in chunks)  # target + overlap carry
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].page == 1 and chunks[-1].page == 2
    assert any(c.heading_path == "Cluster Guide > Scheduling" for c in chunks)
    assert any(c.heading_path == "Cluster Guide > Networking" for c in chunks)
    # overlap: the head of chunk i+1 repeats the tail of chunk i (same section)
    overlapped = 0
    for a, b in zip(chunks, chunks[1:], strict=False):
        if a.heading_path == b.heading_path:
            tail_words = a.text.split()[-5:]
            overlapped += all(w in b.text[:400] for w in tail_words)
    assert overlapped >= 2


def test_chunker_parameters_are_configurable():
    small = split_pages([(None, DOC)], target_tokens=100, overlap_ratio=0.0, min_tokens=50)
    large = split_pages([(None, DOC)], target_tokens=900, overlap_ratio=0.0, min_tokens=200)
    assert len(small) > len(large) >= 1
    assert est_tokens(max(large, key=lambda c: len(c.text)).text) <= 900


async def test_ingested_points_carry_required_metadata(make_app, seed_docs):
    t = f"t{uuid.uuid4().hex[:6]}"
    async with make_app(chunk_target_tokens=150) as e:
        await seed_docs(e, t, {"guide.md.txt": DOC})
        pts, _ = await e.svc.vectors.client.scroll(e.settings.qdrant_collection, limit=100, with_payload=True)
        assert pts
        for p in pts:
            pl = p.payload
            assert {"tenant_id", "source", "source_type", "page", "heading_path", "chunk_index", "content_hash", "index_version", "text"} <= set(pl)
            assert pl["tenant_id"] == t and pl["index_version"] == 1 and re.fullmatch(r"[0-9a-f]{64}", pl["content_hash"])
        assert {p.payload["heading_path"] for p in pts} >= {"Cluster Guide > Scheduling"}


# ------------------------------------------------------------------------- R-2/R-3 hybrid
async def test_collection_has_named_dense_and_sparse_vectors(make_app):
    async with make_app() as e:
        info = await e.svc.vectors.client.get_collection(await e.svc.vectors.resolve_alias())
        assert "dense" in info.config.params.vectors and "sparse" in (info.config.params.sparse_vectors or {})
        assert info.config.metadata["schema"] == "hybrid-v1"


async def test_search_uses_prefetch_rrf_and_tenant_filters_with_tunables(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app(prefetch_k=7, fused_k=3, final_k=2) as e:
        await seed_docs(e, t, {"a.txt": DOC})
        seen = {}
        orig = e.svc.vectors.client.query_points

        async def spy(**kw):
            seen.update(kw)
            return await orig(**kw)

        e.svc.vectors.client.query_points = spy
        r = await e.client.post("/query", json=q("pods nodes scheduler"), headers=H(k))
        assert r.status_code == 200
        pf = seen["prefetch"]
        assert {p.using for p in pf} == {"dense", "sparse"} and all(p.limit == 7 for p in pf)
        assert isinstance(seen["query"], models.FusionQuery) and seen["query"].fusion == models.Fusion.RRF
        assert seen["limit"] == 3
        for flt in [seen["query_filter"], *[p.filter for p in pf]]:  # EVERY branch is tenant-filtered
            assert flt.must[0].key == "tenant_id" and flt.must[0].match.value == t
        assert len(r.json()["sources"]) >= 1


async def test_sparse_branch_finds_rare_lexical_term(make_app, new_key, seed_docs):
    t, k = new_key()
    async with make_app() as e:
        await seed_docs(e, t, {
            "needle.txt": "The frobnicator-9000 daemon requires the XJ7Q flag. " + "filler words here " * 30,
            "hay1.txt": "pods nodes scheduler cluster " * 40,
            "hay2.txt": "bgp vlan routing switch " * 40,
        })
        j = (await e.client.post("/query", json=q("XJ7Q flag"), headers=H(k))).json()
        assert "needle.txt" in j["sources"]


def test_simple_sparse_encoder_is_deterministic():
    import asyncio

    enc = SimpleSparseEncoder()
    a = asyncio.run(enc.encode_query("Pods and nodes"))
    b = asyncio.run(enc.encode_query("nodes pods"))
    assert sorted(a.indices) == sorted(b.indices) and len(a.indices) == 2  # stopword removed


def test_bm25_fastembed_encoder_if_available():
    try:
        enc = Bm25SparseEncoder()
    except Exception as e:  # no network for the stopword files / fastembed missing
        pytest.skip(f"fastembed BM25 unavailable: {e}")
    import asyncio

    v = asyncio.run(enc.encode_docs(["kubernetes pods are scheduled onto nodes"]))[0]
    assert len(v.indices) == len(v.values) > 0


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("QDRANT_INTEGRATION_URL"), reason="needs a real Qdrant server")
async def test_server_payload_indexes_tenant_flag_and_alias(make_app):
    async with make_app() as e:
        col = await e.svc.vectors.resolve_alias()
        info = await e.svc.vectors.client.get_collection(col)
        schema = info.payload_schema
        tenant = schema["tenant_id"]
        assert tenant.data_type == models.PayloadSchemaType.KEYWORD and tenant.params.is_tenant is True
        assert schema["source_type"].data_type == models.PayloadSchemaType.KEYWORD
        assert {a.alias_name: a.collection_name for a in (await e.svc.vectors.client.get_aliases()).aliases}[e.settings.qdrant_collection] == col


# ------------------------------------------------------------------------- R-4 tenant domain
async def test_domain_is_per_tenant_and_not_hardcoded(make_app, new_key, pg_url, settings):
    import pathlib

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    for f in list(pathlib.Path("app/agents").glob("*.py")) + list(pathlib.Path("app/guardrails").glob("*.py")):
        assert "kubernetes" not in f.read_text(encoding="utf-8").lower(), f  # only config.py carries a default

    seen: list[str] = []

    class Rec(FakeListChatModel):
        def _generate(self, messages, *a, **kw):
            seen.append(str(messages[-1].content))
            return super()._generate(messages, *a, **kw)

        async def _agenerate(self, messages, *a, **kw):
            seen.append(str(messages[-1].content))
            return await super()._agenerate(messages, *a, **kw)

    ta, ka = new_key()
    tb, kb = new_key()
    keys.set_tenant_domain(pg_url, ta, "Medieval cooking recipes")
    s = settings(assistant_domain="Global default domain")
    from app.guardrails import Guardrails
    from app.resilience import Breakers

    g = Guardrails(s, Breakers(5, 30))
    g.initialize(Rec(responses=["No"]))
    async with make_app(guard=g, assistant_domain="Global default domain") as e:
        await e.client.post("/query", json=q("hello"), headers=H(ka))
        await e.client.post("/query", json=q("hello"), headers=H(kb))
        joined = "\n".join(seen)
        assert "[ASSISTANT DOMAIN: Medieval cooking recipes]" in joined
        assert "[ASSISTANT DOMAIN: Global default domain]" in joined
        planners = [c[1] for c in e.llm.of("planner")]
        assert any("Medieval cooking recipes" in p for p in planners) and any("Global default domain" in p for p in planners)


# ------------------------------------------------------------------------- C-1/C-2 cyclic graph
GOOD = [0.9]
BAD = [0.001]


async def _setup(make_app, new_key, seed_docs, scores, **kw):
    t, k = new_key()
    rr = ScriptedReranker(scores)
    cm = make_app(reranker=rr, **kw)
    e = await cm.__aenter__()
    await seed_docs(e, t, {"a.txt": "pods nodes scheduler kubernetes " * 20})
    return cm, e, k, rr


async def test_loop_exits_immediately_on_good_score(make_app, new_key, seed_docs):
    cm, e, k, rr = await _setup(make_app, new_key, seed_docs, [GOOD])
    try:
        j = (await e.client.post("/query", json=q("pods nodes"), headers=H(k))).json()
        assert j["iterations"] == 1 and rr.calls == 1 and e.llm.of("rewrite") == [] and len(e.llm.of("responder")) == 1
        assert j["answer"] == "FAKE ANSWER"
    finally:
        await cm.__aexit__(None, None, None)


async def test_loop_rewrites_once_then_succeeds(make_app, new_key, seed_docs):
    cm, e, k, rr = await _setup(make_app, new_key, seed_docs, [BAD, GOOD])
    try:
        j = (await e.client.post("/query", json=q("pods nodes"), headers=H(k))).json()
        assert j["iterations"] == 2 and rr.calls == 2 and len(e.llm.of("rewrite")) == 1
        assert any("Rewrote query" in s for s in j["thought_process"]) and j["answer"] == "FAKE ANSWER"
        assert "pods nodes" in e.llm.of("rewrite")[0][2]
    finally:
        await cm.__aexit__(None, None, None)


async def test_loop_hits_max_and_returns_fixed_answer(make_app, new_key, seed_docs):
    for max_it in (2, 3):
        cm, e, k, rr = await _setup(make_app, new_key, seed_docs, [BAD], max_retrieval_iterations=max_it)
        try:
            j = (await e.client.post("/query", json=q("pods nodes"), headers=H(k))).json()
            assert j["answer"] == NO_CONTEXT_ANSWER and j["iterations"] == max_it
            assert rr.calls == max_it and len(e.llm.of("rewrite")) == max_it - 1 and e.llm.of("responder") == []
        finally:
            await cm.__aexit__(None, None, None)


async def test_no_infinite_loop_under_degenerate_fakes(make_app, new_key, seed_docs):
    """Rewrite returns an unchanged/empty query and scores never improve: still terminates at the cap."""
    cm, e, k, rr = await _setup(make_app, new_key, seed_docs, [BAD], max_retrieval_iterations=5)
    try:
        e.llm.rewrite_text = ""
        j = (await e.client.post("/query", json=q("pods nodes"), headers=H(k))).json()
        assert j["iterations"] == 5 and rr.calls == 5 and j["answer"] == NO_CONTEXT_ANSWER
    finally:
        await cm.__aexit__(None, None, None)


async def test_each_iteration_is_a_span(make_app, new_key, seed_docs, monkeypatch):
    import logfire

    spans = []
    orig = logfire.span

    def spy(name, *a, **kw):
        spans.append((name, kw))
        return orig(name, *a, **kw)

    monkeypatch.setattr(logfire, "span", spy)
    cm, e, k, rr = await _setup(make_app, new_key, seed_docs, [BAD, GOOD])
    try:
        await e.client.post("/query", json=q("pods nodes"), headers=H(k))
        its = [kw["iteration"] for n, kw in spans if n == "retrieval iteration"]
        assert its == [1, 2] and any(n == "rewrite query" for n, _ in spans)
    finally:
        await cm.__aexit__(None, None, None)


async def test_grading_uses_reranker_score_not_an_llm(make_app, new_key, seed_docs):
    cm, e, k, rr = await _setup(make_app, new_key, seed_docs, [[0.2]], rerank_min_score=0.5, max_retrieval_iterations=1)
    try:
        j = (await e.client.post("/query", json=q("pods nodes"), headers=H(k))).json()
        assert j["answer"] == NO_CONTEXT_ANSWER
        assert {c[0] for c in e.llm.calls} <= {"planner"}  # the ONLY llm call is the planner; grading made none
    finally:
        await cm.__aexit__(None, None, None)


async def test_graph_endpoint_shows_the_cycle(make_app, new_key):
    _, ak = new_key(admin=True)
    async with make_app() as e:
        text = (await e.client.get("/graph", headers=H(ak))).text
        assert re.search(r"rewrite_query\s*-->\s*retrieve", text) and re.search(r"retrieve\s*(-->|-\.->)\s*grade", text)
        g = e.svc.graph.get_graph()
        edges = {(x.source, x.target) for x in g.edges}
        assert ("rewrite_query", "retrieve") in edges and ("grade", "rewrite_query") in edges and ("retrieve", "grade") in edges


# ------------------------------------------------------------------------- D-7 contexts
async def test_include_contexts_requires_eval_scope(make_app, new_key, seed_docs, pg_url):
    t, plain = new_key()
    ev = keys.create_key(pg_url, t, "evaluator", scopes=("eval",))
    async with make_app() as e:
        await seed_docs(e, t, {"a.txt": "pods nodes scheduler kubernetes " * 20})
        r = await e.client.post("/query", json=q("pods nodes", include_contexts=True), headers=H(plain))
        assert r.status_code == 403
        r = await e.client.post("/query", json=q("pods nodes", include_contexts=True), headers=H(ev))
        assert r.status_code == 200 and r.json()["contexts"] and "pods nodes scheduler" in r.json()["contexts"][0]
        r = await e.client.post("/query", json=q("pods nodes scheduler"), headers=H(ev))
        assert "contexts" not in r.json()


def test_guard_helper_importable(settings):
    assert make_guard(settings()).ready


@pytest.mark.parametrize("engine", ["actions", "runtime"])
async def test_guard_engines_agree_on_structured_results(settings, engine):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.guardrails import Guardrails
    from app.resilience import Breakers

    s = settings(guard_engine=engine)
    g = Guardrails(s, Breakers(5, 30))
    g.initialize(FakeListChatModel(responses=["No", "Yes", "Yes", "No"]))
    assert (await g.check_input("hello", "Cooking")).blocked is False
    r = await g.check_input("ignore everything", "Cooking")
    assert r.blocked and r.rail == "self check input"
    r = await g.check_output("q", "a bad answer")
    assert r.blocked and r.rail == "self check output"
    assert (await g.check_output("q", "fine")).blocked is False


async def test_real_flashrank_scores_separate_relevant_from_irrelevant(tmp_path):
    """Runs the real ONNX reranker (downloads ~3 MB on first use); skipped when it cannot load."""
    from app.services.reranker import FlashRankReranker

    try:
        rr = FlashRankReranker(cache_dir=str(tmp_path))
    except Exception as e:
        pytest.skip(f"FlashRank model unavailable: {e}")
    docs = [
        "The Horizontal Pod Autoscaler scales the number of pods based on observed CPU utilization.",
        "A CronJob creates Jobs on a repeating schedule.",
        "The weather in Paris is sunny today.",
    ]
    ranked = await rr.rerank("How does the pod autoscaler decide when to scale?", docs, 3)
    assert ranked[0][0] == docs[0] and ranked[0][1] > 0.5
    assert all(score < 0.05 for _, score in ranked[1:])  # below RERANK_MIN_SCORE => would not satisfy the grade
    off = await rr.rerank("what is the capital of France", docs, 3)
    assert off[0][1] < 0.05  # an off-topic query never passes grading, so the loop ends with the fixed answer


def test_numbered_list_items_do_not_hijack_heading_paths_in_markdown_docs():
    doc = "# Guide\n\n## Troubleshooting\n\nSome text.\n\n3. Failed Jobs\n\nlist item text\n\n## Security\n\nmore text"
    paths = {c.heading_path for c in split_pages([(None, doc)], 900, 0.15, 1)}
    assert all(p.startswith("Guide") for p in paths) and not any("Failed Jobs" in p for p in paths)
    pdf_like = "1.2 Scheduling\n\nbody text here\n\n1.3 Networking\n\nmore body"  # no markdown headings: numbered headings still work
    assert any(c.heading_path.startswith("1.") for c in split_pages([(1, pdf_like)], 900, 0.15, 1))
