"""Build web/server/benchmarks.json (the landing page's headline numbers) from the repository's own result files.

    python scripts/build_benchmarks.py            # write the file
    python scripts/build_benchmarks.py --check    # exit 1 if the committed file is stale (used by a test)

Every figure is read from a measured artefact; nothing is typed in here. A number whose source file is missing is
left out rather than guessed. The faithfulness metric has no completed run on record (the RAGAS judge's daily token
cap stopped it), so it is deliberately absent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "server" / "benchmarks.json"

RETRIEVAL = ROOT / "loadtest" / "retrieval_eval_result.json"
GUARD = ROOT / "evals" / "guardrails_calibration_after.json"
CHAOS = ROOT / "evals" / "chaos_drill_2026-10-11.json"
CAPACITY = ROOT / "DOCS" / "12_CAPACITY.md"


def pct(x: float) -> str:
    return f"{x:g}%"


def retrieval() -> dict | None:
    if not RETRIEVAL.exists():
        return None
    d = json.loads(RETRIEVAL.read_text(encoding="utf-8"))
    cell = d["cells"][d["default_config"]]
    return {
        "id": "retrieval", "label": "Retrieval hit@5", "value": pct(cell["hit@5"]),
        "sub": f"hit@1 {pct(cell['hit@1'])} · MRR {pct(cell['mrr'])} · {d['n_labels']} labelled questions",
        "env": f"real Gemini embeddings + BM25, default config ({d['default_config'].replace('|', ', ')})",
        "source": "loadtest/retrieval_eval_result.json",
    }


def guardrails() -> dict | None:
    if not GUARD.exists():
        return None
    d = json.loads(GUARD.read_text(encoding="utf-8"))
    fp, fb = d["false_pass"], d["false_block"]
    return {
        "id": "guardrails", "label": "Guardrail false-pass", "value": pct(fp["rate_pct"]),
        "sub": f"{fp['n']}/{fp['of']} attacks got through · {fb['n']}/{fb['of']} legitimate questions wrongly blocked",
        "env": f"{d['cases']} labelled cases, guard model {d['guard_model']}",
        "source": "evals/guardrails_calibration_after.json",
    }


def throughput() -> dict | None:
    if not CAPACITY.exists():
        return None
    for line in CAPACITY.read_text(encoding="utf-8").splitlines():
        if line.startswith("| 3 |"):
            cells = [c.strip().replace("*", "").replace(",", "") for c in line.strip("|").split("|")]
            rps = float(cells[3].split("/")[-1])
            p95 = float(cells[5].split("/")[-1]) / 1000
            err = float(cells[-1])
            users = cells[2].split("/")[-1].strip()
            return {
                "id": "throughput", "label": "Load-test throughput", "value": f"{rps:g} RPS",
                "sub": f"p95 {p95:.1f} s at {users} concurrent users · {err:g}% errors · simulated LLM, one laptop",
                "env": "load test on one laptop with SIMULATED LLM and embeddings; real Redis, Postgres, Qdrant",
                "source": "DOCS/12_CAPACITY.md (run 3)",
            }
    return None


def chaos() -> dict | None:
    if not CHAOS.exists():
        return None
    d = json.loads(CHAOS.read_text(encoding="utf-8"))
    if not d.get("api_still_running"):
        return None
    worst = max(d["recovery"].values())
    return {
        "id": "resilience", "label": "Survives dependency outages", "value": "0 restarts",
        "sub": f"Redis, Qdrant and Postgres were killed under load; /readyz recovered in ≤ {worst:g} s each",
        "env": "chaos drill against real datastores, API process never restarted",
        "source": "evals/chaos_drill_2026-10-11.json",
    }


def build() -> dict:
    metrics = [m for m in (retrieval(), guardrails(), throughput(), chaos()) if m]
    return {"metrics": metrics}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    fresh = build()
    text = json.dumps(fresh, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not OUT.exists() or json.loads(OUT.read_text(encoding="utf-8")) != fresh:
            print("web/server/benchmarks.json is stale: run python scripts/build_benchmarks.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(fresh['metrics'])} metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
