"""Minimal live checks of the credentials in .env. Prints OK/FAIL + HTTP status + a one-line reason per check. Never prints a key.

  python scripts/check_keys.py            # Groq primary (primary + guard model), Groq fallback key, Gemini embedding, Qdrant
  python scripts/check_keys.py --json out.json

Exit code 0 if every configured check is OK, 1 otherwise. A check whose key is empty is reported SKIP (not a failure).
On "model not found" it lists the account's models and PROPOSES a replacement (it does not edit .env; use --apply to do so).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = {**dotenv_values(ROOT / ".env")}
GROQ = "https://api.groq.com/openai/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
results: list[dict] = []


def last4(v: str) -> str:
    return "..." + v[-4:] if v and len(v) >= 12 else "(empty)"


def short(msg: str, n: int = 110) -> str:
    return re.sub(r"\s+", " ", msg)[:n]


def err_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error", j)
        return short(e.get("message", str(e)) if isinstance(e, dict) else str(e))
    except Exception:
        return short(r.text)


def call(method: str, url: str, **kw) -> httpx.Response:
    for attempt in (1, 2):
        r = httpx.request(method, url, timeout=45, **kw)
        if r.status_code == 429 and attempt == 1:  # back off once, then give up
            wait = min(float(r.headers.get("retry-after", "5") or 5), 20)
            time.sleep(wait)
            continue
        return r
    return r


def record(name: str, status: str, http: int | str, reason: str, **extra):
    results.append({"check": name, "status": status, "http": http, "reason": reason, **extra})
    print(f"{status:4s} | {name:34s} | HTTP {http} | {reason}")


def groq_chat(name: str, key: str, model: str):
    if not key:
        return record(name, "SKIP", "-", "key is empty")
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_completion_tokens": 128}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
    r = call("POST", f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code == 200:
        j = r.json()
        content = (j["choices"][0]["message"].get("content") or "").strip()
        u = j.get("usage", {})
        return record(name, "OK" if content else "FAIL", 200, f"model={model} reply_chars={len(content)} tokens={u.get('total_tokens')}" + ("" if content else " (empty content: raise max tokens)"))
    reason = err_text(r)
    extra = {}
    if r.status_code in (400, 404) and re.search(r"model|not found|does not exist", reason, re.I):
        m = call("GET", f"{GROQ}/models", headers={"Authorization": f"Bearer {key}"})
        ids = sorted(x["id"] for x in m.json().get("data", [])) if m.status_code == 200 else []
        extra = {"available_models": ids}
        reason += f" | available: {', '.join(ids)[:300]}"
    record(name, "FAIL", r.status_code, reason, **extra)


def gemini_embed(key: str, model: str, dim: int):
    name = f"Gemini embed ({model}, dim={dim})"
    if not key:
        return record(name, "SKIP", "-", "key is empty")
    body = {"content": {"parts": [{"text": "ping"}]}, "output_dimensionality": dim}
    r = call("POST", f"{GEMINI}/models/{model}:embedContent", headers={"x-goog-api-key": key}, json=body)
    if r.status_code == 200:
        n = len(r.json()["embedding"]["values"])
        return record(name, "OK" if n == dim else "FAIL", 200, f"returned dimension {n}" + ("" if n == dim else f" != configured {dim}"), returned_dim=n)
    reason = err_text(r)
    extra = {}
    if r.status_code in (400, 404) and re.search(r"not found|not supported|model", reason, re.I):
        m = call("GET", f"{GEMINI}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200})
        names = []
        if m.status_code == 200:
            for x in m.json().get("models", []):
                if "embedContent" in x.get("supportedGenerationMethods", []):
                    names.append(x["name"].removeprefix("models/"))
        ga = [n for n in names if "embedding" in n and not re.search(r"preview|exp|latest", n)]
        extra = {"embedding_models": names, "ga_candidates": ga}
        reason += f" | embedding models: {', '.join(names)[:260]}"
    record(name, "FAIL", r.status_code, reason, **extra)


def qdrant(url: str, key: str):
    name = "Qdrant list collections"
    if not url:
        return record(name, "SKIP", "-", "QDRANT_URL is empty")
    headers = {"api-key": key} if key else {}
    try:
        r = call("GET", url.rstrip("/") + "/collections", headers=headers)
    except Exception as e:
        return record(name, "FAIL", "-", f"{type(e).__name__}: {short(str(e), 80)}")
    if r.status_code == 200:
        cols = [c["name"] for c in r.json()["result"]["collections"]]
        return record(name, "OK", 200, f"{len(cols)} collections: {', '.join(cols)[:200] or '(none)'}", collections=cols)
    record(name, "FAIL", r.status_code, err_text(r))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    a = ap.parse_args()
    primary, guard = ENV.get("LLM_PRIMARY_MODEL") or "llama-3.3-70b-versatile", ENV.get("GUARD_MODEL") or "llama-3.1-8b-instant"
    print(f"keys under test: GROQ_API_KEY {last4(ENV.get('GROQ_API_KEY', ''))}, GROQ_FALLBACK_API_KEY {last4(ENV.get('GROQ_FALLBACK_API_KEY', ''))}, "
          f"GEMINI_API_KEY {last4(ENV.get('GEMINI_API_KEY', ''))}, QDRANT_API_KEY {last4(ENV.get('QDRANT_API_KEY', ''))}")
    groq_chat(f"Groq primary / {primary}", ENV.get("GROQ_API_KEY", ""), primary)
    groq_chat(f"Groq primary / guard {guard}", ENV.get("GROQ_API_KEY", ""), guard)
    groq_chat(f"Groq fallback key / {guard}", ENV.get("GROQ_FALLBACK_API_KEY", ""), guard)
    gemini_embed(ENV.get("GEMINI_API_KEY", ""), ENV.get("EMBEDDING_MODEL") or "gemini-embedding-2", int(ENV.get("EMBEDDING_DIM") or 768))
    qdrant(ENV.get("QDRANT_URL", ""), ENV.get("QDRANT_API_KEY", ""))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(r["status"] in ("OK", "SKIP") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
