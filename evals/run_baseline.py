"""Headless RAGAS baseline: query the real pipeline, then score it with the real Groq judge.

    PYTHONPATH=. python evals/run_baseline.py [--out evals/baseline_<date>.json] [--budget 400]
                                              [--phase1-json evals/.phase1.json] [--skip-phase1]

Phase 1 runs the app IN-PROCESS over httpx/ASGI (no server to start, no port to guess) against the real Groq,
Gemini and local Postgres/Redis/Qdrant, and records the answer, the retrieved contexts and the detected tool for
every golden sample. Phase 2 hands those to `evals.metrics.run_all_metrics`, which uses JUDGE_GROQ (so an eval run
cannot drain the production key) with the cooldowns that tier needs.

Every judge request is counted and the run aborts if it would pass --budget. The result file holds per-sample
scores, the means, the judge-call count and enough provenance to tell two runs apart.

Phase 2 checkpoints each metric to --checkpoint as it finishes, so an interrupted run (the full six metrics take
over an hour of cooldowns) resumes instead of paying for the finished metrics again. The checkpoint is keyed to
the exact answers it scored, so it is discarded automatically once the pipeline's output changes; --fresh forces
that too.
Secrets are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
for _stream in (sys.stdout, sys.stderr):
    # evals/metrics.py puts emoji in its progress messages and a redirected stdout on Windows is cp1252,
    # so printing the first one raises UnicodeEncodeError and kills the run before any metric is scored.
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - not a real stream (pytest capture); nothing to reconfigure
        pass

from dotenv import load_dotenv  # noqa: E402

from evals.pipeline import detect_tool, load_golden_dataset  # noqa: E402

load_dotenv()  # evals/metrics.py reads JUDGE_GROQ from the environment, not from Settings


def pick_judge_key() -> str:
    """Which key the judge uses, as a NAME (never a value).

    Order: JUDGE_GROQ, then GROQ_FALLBACK_API_KEY, then GROQ_API_KEY. The point of the fallback is that a long eval
    run must not eat the rate limit of the key serving requests - and the judge model is a different model, so it
    gets its own tokens-per-minute bucket on whichever key it lands.
    """
    for name in ("JUDGE_GROQ", "GROQ_FALLBACK_API_KEY", "GROQ_API_KEY"):
        if (os.environ.get(name) or "").strip():
            os.environ["JUDGE_GROQ"] = os.environ[name].strip()
            return name
    raise SystemExit("run_baseline: no Groq key available for the judge (set JUDGE_GROQ)")

RESPONSE_TRUNCATE = 300  # same as evals/pipeline.py: the judge sees what the UI would show
CONTEXTS_KEPT = 5
EVAL_DOMAIN = ("Kubernetes (architecture, Jobs, CronJobs, autoscaling, work queues), container workloads and "
               "Databricks job automation")  # the actual subject matter of DATA/true_data


class JudgeBudget:
    """Counts judge requests and refuses to go over budget (raises inside the metric call)."""

    def __init__(self, limit: int):
        self.limit, self.n = limit, 0

    def tick(self):
        self.n += 1
        if self.n > self.limit:
            raise RuntimeError(f"judge-call budget exceeded ({self.n} > {self.limit})")


def install_budget(budget: JudgeBudget):
    """Wrap AsyncOpenAI.chat.completions.create so every judge request is counted exactly once."""
    from openai.resources.chat import completions

    orig = completions.AsyncCompletions.create

    async def counted(self, *a, **kw):
        budget.tick()
        return await orig(self, *a, **kw)

    completions.AsyncCompletions.create = counted


async def phase1(golden: dict, delay_s: float) -> dict:
    """Ask the real pipeline every golden question, in-process.

    The corpus is ingested FOR THIS TENANT first. Qdrant payloads are tenant-scoped, so a fresh eval tenant
    retrieves nothing and the pipeline answers "not enough information" for all 15 questions - which the judge
    would happily score as perfectly faithful. Pass EVAL_TENANT + EVAL_API_KEY to reuse an already-ingested
    tenant instead (then set EVAL_SKIP_INGEST=1).
    """
    import copy

    import httpx
    from asgi_lifespan import LifespanManager

    from app.api import create_app
    from app.auth import keys
    from app.config import load_settings
    from app.db.migrate import migrate
    from app.ingestion.pipeline import ingest

    s = load_settings()
    migrate(s.direct_database_url or s.database_url)
    tenant = os.environ.get("EVAL_TENANT") or f"eval{uuid.uuid4().hex[:6]}"
    key = os.environ.get("EVAL_API_KEY") or keys.create_key(
        s.database_url, tenant, "evaluator", rate_per_min=1000, scopes=("eval",), redis_url=s.redis_url)
    # Pin the tenant's topic policy to the corpus instead of inheriting ASSISTANT_DOMAIN: the input rail blocks
    # off-domain questions, so a domain that does not mention Databricks refuses 3 of the 15 golden questions and
    # the baseline silently measures the rail instead of the responder.
    keys.set_tenant_domain(s.direct_database_url or s.database_url, tenant, EVAL_DOMAIN)
    out = copy.deepcopy(golden)
    samples = out["rag_samples"]
    app = create_app(s)
    ingested = None
    # asgi_lifespan defaults to a 5 s startup timeout, which this app blows past: startup builds the NeMo
    # Guardrails engine, the Qdrant client and the Postgres pool. Shutdown drains the same.
    async with LifespanManager(app, startup_timeout=180, shutdown_timeout=60) as mgr:
        svc = app.state.svc
        if not os.environ.get("EVAL_SKIP_INGEST"):
            rep = await ingest(svc.vectors, svc.embedder, svc.sparse, s, tenant, "DATA/true_data", "true")
            ingested = {"files_ok": rep.files_ok, "chunks_total": rep.chunks_total,
                        "chunks_embedded": rep.chunks_embedded, "failed": [f"{p}: {why}" for p, why in rep.failed]}
            print(f"  ingested for tenant: {ingested}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mgr.app), base_url="http://eval", timeout=180) as c:
            for i, smp in enumerate(samples):
                t0 = time.perf_counter()
                r = await c.post("/query", headers={"X-API-Key": key},
                                 json={"q": smp["question"], "session_id": f"baseline_{i}_{uuid.uuid4().hex[:6]}",
                                       "include_contexts": True})
                if r.status_code != 200:
                    print(f"  [{i + 1}/{len(samples)}] HTTP {r.status_code}: {r.text[:160]}")
                    smp["actual_response"] = ""
                    smp["actual_contexts"] = smp.get("relevant_contexts", [])
                    smp["actual_tools_called"] = ["unknown"]
                else:
                    d = r.json()
                    smp["actual_response"] = (d.get("answer") or "")[:RESPONSE_TRUNCATE]
                    smp["actual_contexts"] = (d.get("contexts") or [])[:CONTEXTS_KEPT]
                    smp["actual_tools_called"] = [detect_tool(d.get("thought_process") or [])]
                    smp["_cache_hit"] = bool(d.get("cache_hit"))
                    smp["_blocked"] = bool(d.get("blocked"))
                    print(f"  [{i + 1}/{len(samples)}] {round(1000 * (time.perf_counter() - t0))} ms, "
                          f"{len(smp['actual_contexts'])} contexts, tool={smp['actual_tools_called'][0]}, "
                          f"cache_hit={d.get('cache_hit')}")
                if i < len(samples) - 1 and delay_s:
                    await asyncio.sleep(delay_s)
    out["_phase1"] = {"tenant": tenant, "mode": "gateway" if s.use_gateway else "direct-groq",
                      "ingested": ingested,
                      "with_contexts": sum(bool(x.get("actual_contexts")) for x in samples),
                      "from_cache": sum(bool(x.get("_cache_hit")) for x in samples),
                      "blocked": sum(bool(x.get("_blocked")) for x in samples),
                      "models": {"primary": s.llm_primary_model, "fallback": s.llm_fallback_model,
                                 "guard": s.guard_model, "embedding": f"{s.embedding_model}/{s.embedding_dim}"}}
    return out


def gate_phase1(data: dict) -> str | None:
    """Reason to refuse scoring, or None.

    A run where retrieval returned nothing still produces fluent "I don't have enough information" answers, and
    the judge scores those as perfectly faithful - a baseline that measures the judge, not the pipeline. Same for
    answers replayed from the semantic cache. Both have already happened on this project; fail loudly instead.
    """
    p1 = data.get("_phase1") or {}
    n = len(data["rag_samples"])
    if (p1.get("from_cache") or 0) > 0:
        return f"{p1['from_cache']}/{n} answers came from the semantic cache"
    with_ctx = p1.get("with_contexts")
    if with_ctx is not None and with_ctx < n - 2:
        return f"only {with_ctx}/{n} answers had retrieved context (tenant corpus missing?)"
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    today = dt.date.today().isoformat()
    ap.add_argument("--out", default=f"evals/baseline_{today}.json")
    ap.add_argument("--budget", type=int, default=400, help="hard cap on judge requests")
    ap.add_argument("--phase1-json", default="evals/.phase1.json", help="where the live answers are cached")
    ap.add_argument("--skip-phase1", action="store_true", help="reuse --phase1-json instead of querying the pipeline")
    ap.add_argument("--delay", type=float, default=float(os.environ.get("EVAL_DELAY_S", "4")),
                    help="seconds between live queries (provider rate limits)")
    ap.add_argument("--checkpoint", default="evals/.phase2_checkpoint.json",
                    help="per-metric resume file; metrics already scored for these exact answers are reused")
    ap.add_argument("--fresh", action="store_true", help="ignore and overwrite the phase-2 checkpoint")
    a = ap.parse_args()

    p1 = pathlib.Path(a.phase1_json)
    if a.skip_phase1:
        if not p1.exists():
            print(f"run_baseline: {p1} not found; run without --skip-phase1 first", file=sys.stderr)
            return 2
        data = json.loads(p1.read_text(encoding="utf-8"))
        print(f"phase 1: reusing {p1}")
    else:
        print("phase 1: querying the real pipeline in-process")
        data = await phase1(load_golden_dataset(), a.delay)
        p1.write_text(json.dumps(data, indent=2), encoding="utf-8")
    answered = [s for s in data["rag_samples"] if (s.get("actual_response") or "").strip()]
    print(f"phase 1: {len(answered)}/{len(data['rag_samples'])} samples answered")
    if not answered:
        print("run_baseline: no sample produced an answer; nothing to score", file=sys.stderr)
        return 1
    bad = gate_phase1(data)
    if bad:
        print(f"run_baseline: refusing to score - {bad}", file=sys.stderr)
        return 1

    # Only samples the pipeline actually grounded get scored. evals.metrics falls back to the GOLDEN
    # relevant_contexts when actual_contexts is empty, which would score a "couldn't find it" refusal against
    # context the responder never saw - a number that looks like faithfulness but measures nothing.
    scored = [s for s in answered if s.get("actual_contexts")]
    kept_ids = {id(s) for s in scored}
    excluded = [{"id": s.get("id"), "question": s["question"][:70],
                 "why": "blocked by input rail" if s.get("_blocked") else "no context retrieved"}
                for s in data["rag_samples"] if id(s) not in kept_ids]
    if excluded:
        print(f"phase 2: excluding {len(excluded)} ungrounded sample(s) from the metrics: "
              + "; ".join(f"{e['id']} ({e['why']})" for e in excluded))
    n_total = len(data["rag_samples"])
    data = {**data, "rag_samples": scored}

    key_name = pick_judge_key()
    print(f"phase 2: judge key from {key_name}")
    budget = JudgeBudget(a.budget)
    install_budget(budget)
    from evals.metrics import JUDGE_MODEL, run_all_metrics

    ckpt_path = pathlib.Path(a.checkpoint) if a.checkpoint else None
    if a.fresh and ckpt_path and ckpt_path.exists():
        ckpt_path.unlink()
        print(f"phase 2: discarded the checkpoint at {ckpt_path} (--fresh)")

    t0 = time.perf_counter()
    prov: dict = {}
    dfs = await run_all_metrics(data, status_cb=lambda m: print("  " + m, flush=True),
                               checkpoint=ckpt_path, calls_so_far=lambda: budget.n, provenance=prov)
    means = {k: round(float(df[df.columns[-1]].mean()), 3) for k, df in dfs.items()}
    # Judge calls come in two flavours once a run can resume: what THIS process spent, and what the whole
    # baseline cost across the interrupted attempts that fed the checkpoint. Report both; the night budget is
    # charged the total.
    per_metric = prov.get("calls_by_metric") or {}
    total_calls = sum(v for v in per_metric.values() if isinstance(v, int))
    result = {
        "run_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "judge": {"model": JUDGE_MODEL, "key_from": key_name, "calls": budget.n,
                  "calls_all_attempts": total_calls, "calls_by_metric": per_metric,
                  "resumed_metrics": prov.get("resumed_metrics") or [], "budget": a.budget},
        "pipeline": data.get("_phase1", {}),
        "samples_scored": len(scored),
        "samples_total": n_total,
        "samples_excluded": excluded,
        "wall_clock_s": round(time.perf_counter() - t0),
        "means": means,
        "per_sample": {k: df.to_dict(orient="records") for k, df in dfs.items()},
    }
    pathlib.Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\n" + json.dumps({"means": means, "judge_calls_this_run": budget.n,
                             "judge_calls_all_attempts": total_calls,
                             "resumed": result["judge"]["resumed_metrics"], "out": a.out}, indent=2))
    gates = {"faithfulness": 0.8, "answer_relevancy": 0.8}
    below = {k: means[k] for k, t in gates.items() if k in means and means[k] < t}
    if below:
        print(f"\nBELOW TARGET (>= 0.8 each): {below}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
