"""Record the landing page's scenarios from the REAL system into web/public/demo/chips.json.

    python scripts/capture_demo.py --bff-a http://127.0.0.1:8787 --bff-b http://127.0.0.1:8788 [--only normal exact] [--pace 70]

Every question is sent through a BFF (so the stored answer and trace went through the same allowlist as live traffic), against
tenant A (the Kubernetes documents) and, for the isolation scenario, tenant B (different documents, same domain wording).
The fixtures make the page instant and spend no quota when visitors click them. Questions here must match web/src/demo/chips.ts.

Free-tier Groq allows ~8K tokens/minute per model, so calls that reach the answer model are spaced by --pace seconds.
Keys never appear in the output file or on screen; only answers and sanitised traces are written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "public" / "demo" / "chips.json"

A, B = "a", "b"
# id -> (layout, [(tenant, label, question), ...])  - one entry per case shown on the page
SCENARIOS: dict[str, tuple[str, list[tuple[str, str | None, str]]]] = {
    "normal": ("single", [(A, None, "How does the horizontal pod autoscaler decide when to scale pods?")]),
    "exact": ("single", [(A, None, "What does backoffLimit do?")]),
    "followup": ("turns", [
        (A, "Turn 1", "What is a Kubernetes CronJob?"),
        (A, "Turn 2 (a follow-up)", "How do I schedule it to run every five minutes?"),
    ]),
    "unknown": ("single", [(A, None, "How do I configure Istio mutual TLS between services?")]),
    "offtopic": ("single", [(A, None, "Write me a poem about the ocean.")]),
    "pii": ("single", [(A, None, "I am jane.doe@example.com. How does the horizontal pod autoscaler decide when to scale?")]),
    "tenants": ("compare", [
        (A, "Tenant A · Kubernetes documents", "What is the Data Plane Development Kit (DPDK) used for?"),
        (B, "Tenant B · networking papers", "What is the Data Plane Development Kit (DPDK) used for?"),
    ]),
}


def ask(client: httpx.Client, base: str, question: str, session: str) -> dict:
    r = client.post(f"{base}/api/query", json={"q": question, "session_id": session}, headers={"Origin": base}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    body.pop("request_id", None)
    return body


def spent_llm(body: dict) -> int:
    return int(((body.get("trace") or {}).get("llm_calls")) or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bff-a", required=True)
    ap.add_argument("--bff-b", required=True)
    ap.add_argument("--only", nargs="*", help="record only these scenario ids (merged into the existing file)")
    ap.add_argument("--pace", type=float, default=70.0, help="seconds to wait after a call that used the answer model")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out = Path(args.out)
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() and args.only else {"chips": {}}
    chips = dict(existing.get("chips", {}))
    bases = {A: args.bff_a.rstrip("/"), B: args.bff_b.rstrip("/")}
    failures: list[str] = []
    total_llm = 0
    with httpx.Client() as client:
        for sid, (layout, cases) in SCENARIOS.items():
            if args.only and sid not in args.only:
                continue
            recorded = []
            try:
                for tenant, label, question in cases:
                    body = ask(client, bases[tenant], question, f"cap-{sid}-{tenant}")  # ONE session per scenario: a follow-up needs the history
                    recorded.append({"label": label, "question": question, "response": body})
                    total_llm += spent_llm(body)
                    steps = [s["step"] for s in (body.get("trace") or {}).get("steps", [])]
                    print(f"[{sid}/{tenant}] blocked={body.get('blocked')} iter={body.get('iterations')} llm_calls={spent_llm(body)} "
                          f"sources={body.get('sources')} :: {body['answer'][:110]!r}\n      steps={steps}")
                    if any(s.get("step") == "llm" for s in (body.get("trace") or {}).get("steps", [])):
                        time.sleep(args.pace)
            except Exception as e:  # noqa: BLE001 - report and continue with the other scenarios
                failures.append(f"{sid}: {e}")
                print(f"[{sid}] FAILED: {e}", file=sys.stderr)
                continue
            chips[sid] = {"layout": layout, "cases": recorded}

    if failures:
        print("\nNot written (failures):\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    payload = {"recorded_at": dt.date.today().isoformat(), "chips": chips}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"\nwrote {out}: {len(chips)} scenarios, {total_llm} model calls this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
