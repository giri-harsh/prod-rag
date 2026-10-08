"""Turn live_results.json (written by tests/live) into live_report.md.   python scripts/make_live_report.py [in.json] [out.md]"""
import json
import sys
from pathlib import Path

src = Path(sys.argv[1] if len(sys.argv) > 1 else "live_results.json")
dst = Path(sys.argv[2] if len(sys.argv) > 2 else "live_report.md")
if not src.exists():
    sys.exit(f"make_live_report: {src} not found (did the live tests run?)")
d = json.loads(src.read_text(encoding="utf-8"))
e = d.get("end_to_end")
if not e:
    sys.exit("make_live_report: end_to_end results missing (the live end-to-end test failed or was skipped)")
pf = d.get("portkey_fallback", {})
df = d.get("direct_key_failover", {})

L = ["# Live verification report", "",
     "Real Groq / Portkey / Gemini, real Postgres / Redis / Qdrant. Numbers below come from this run only.", "",
     "## Guardrails", "",
     "| set | n | result | rate |", "|---|---|---|---|",
     f"| true-data questions (should be answered) | {e['true_data']['n']} | {e['true_data']['false_blocks']} wrongly blocked | **false-block {e['true_data']['false_block_rate_pct']}%** |",
     f"| off-topic (should be refused) | {e['off_topic']['n']} | {e['off_topic']['passed_through']} answered | **false-pass {e['off_topic']['false_pass_rate_pct']}%** |",
     f"| jailbreak / injection (should be refused) | {e['attacks']['n']} | {e['attacks']['passed_through']} answered | **false-pass {e['attacks']['false_pass_rate_pct']}%** |",
     f"| noisy-data questions | {e['noisy']['n']} | {e['noisy']['answered']} answered | n/a |", ""]
if "with_retrieved_context" in e["true_data"]:
    L += [f"Of the {e['true_data']['n']} true-data questions, {e['true_data']['with_retrieved_context']} were answered from retrieved context and "
          f"{e['true_data']['from_semantic_cache']} from the semantic cache. The tenant's semantic and planner caches are cleared before the run, so a "
          f"pass means the full pipeline (retrieval, grading, responder, output rail) really ran rather than being replayed from cache.", ""]
L += [
     f"## Run mode: **{e.get('mode', '?')}**   models: `{e.get('models', {})}`", "",
     "## Portkey fallback (gateway mode; primary slug forced invalid)", "",
     f"- result: **{pf.get('status') or ('PASS' if pf.get('ok') else 'not run')}**" + (f", latency {pf.get('latency_ms')} ms" if pf.get("latency_ms") else "") + (f", error {pf.get('error')}: {pf.get('detail')}" if pf.get("error") else ""), "",
     "## Direct-Groq key failover (primary key forced invalid -> GROQ_FALLBACK_API_KEY)", "",
     f"- result: **{df.get('status') or 'not run'}**" + (f", latency {df.get('latency_ms')} ms" if df.get("latency_ms") else "") + (f", error {df.get('error')}: {df.get('detail')}" if df.get("error") else ""), "",
     "## Groundedness spot checks", "",
     "`overlap` = share of the answer's content words found in the retrieved context (a heuristic - read the answers yourself).", ""]
for s in e["groundedness_spot_checks"]:
    L += [f"- **Q:** {s['question']}", f"  - answer: {s['answer']!r}", f"  - contexts: {s['contexts']}, overlap: {s['overlap']}, sources: {s['sources']}"]
L += ["", "## Latency", "", f"Per request (all {sum(e[k]['n'] for k in ('true_data', 'off_topic', 'attacks', 'noisy'))} requests): p50 {e['latency_request_ms']['p50']} ms, p95 {e['latency_request_ms']['p95']} ms, max {e['latency_request_ms']['max']} ms.", "",
      "| stage | calls | mean ms | p95 ms |", "|---|---|---|---|"]
for k, v in sorted(e["latency_stages"].items()):
    L.append(f"| {k} | {v['n']} | {v['mean_ms']} | {v['p95_ms']} |")
t = e.get("transient_503")
if t:
    L += ["", "## Transient 503s", "",
          f"- /query calls retried after a 503 (fail-closed guardrail or upstream): **{t['retries_used']}** of at most {t['retry_cap']} allowed per run.",
          f"- failed provider calls underneath those 503s: {t.get('failed_calls')}" + (f"; first reasons: {t['reasons']}" if t.get("reasons") else ".")]
b = e["budget"]
L += ["", "## Budget", "", f"- chat completions by the test: {b['llm_calls_used_by_test']} / {b['llm_budget']}; guard-model calls: {b.get('guard_calls_used', '?')} / {b.get('guard_budget', '?')}; embeddings by the test: {b['embeddings_used_by_test']} / {b['embedding_budget']}"
      + (" (sample data was ingested beforehand by the verify script)." if e.get("tenant_preingested") else ".")]
dst.write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"wrote {dst}")
