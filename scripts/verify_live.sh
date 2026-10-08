#!/usr/bin/env bash
# Verify the system against REAL Groq / Portkey / Gemini.  Usage: bash scripts/verify_live.sh
# Needs .env with GROQ_API_KEY, PORTKEY_API_KEY, GEMINI_API_KEY (values are never printed). Idempotent; exits non-zero with one line on failure.
set -uo pipefail
cd "$(dirname "$0")/.."
fail() { echo "verify_live FAILED: $1" >&2; exit 1; }

[ -f .env ] || fail ".env not found - copy .env.example to .env and fill the three API keys"
env_val() {  # value of KEY from .env (never echoed by callers)
  grep -E "^[[:space:]]*$1[[:space:]]*=" .env | tail -1 | sed -E "s/^[^=]*=//; s/[[:space:]]+#.*$//; s/^[[:space:]\"']+//; s/[[:space:]\"']+$//"
}
for k in GROQ_API_KEY PORTKEY_API_KEY GEMINI_API_KEY; do
  [ -n "$(env_val $k)" ] || fail "$k is empty in .env"
done

if [ -x .venv/bin/python ]; then PY=.venv/bin/python; elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=${PYTHON:-python}; fi
"$PY" -c "import app.config" 2>/dev/null || fail "cannot import the app with $PY (create the venv: uv venv --python 3.11 .venv && uv pip install -r requirements-dev.lock)"

# datastores: use the ones configured in .env / the environment, otherwise bring up the compose ones (ports published on localhost)
if [ -z "${DATABASE_URL:-}$(env_val DATABASE_URL)" ] || [ -z "${REDIS_URL:-}$(env_val REDIS_URL)" ] || [ -z "${QDRANT_URL:-}$(env_val QDRANT_URL)" ]; then
  command -v docker >/dev/null 2>&1 || fail "DATABASE_URL/REDIS_URL/QDRANT_URL are not set in .env and docker is not available to start them"
  docker compose up -d --wait postgres redis qdrant >/dev/null 2>&1 || fail "could not start postgres/redis/qdrant with docker compose"
  export DATABASE_URL=postgresql://rag:rag@127.0.0.1:5432/rag REDIS_URL=redis://127.0.0.1:6379/0 QDRANT_URL=http://127.0.0.1:6333
fi

export LIVE_REPORT_JSON=live_results.json
rm -f "$LIVE_REPORT_JSON" live_report.md
TENANT=verifylive
"$PY" -m app.db.migrate >/dev/null || fail "migrations failed"
KEY=$("$PY" -m app.auth.cli create --tenant "$TENANT" --user tester --scope eval --label verify_live 2>/dev/null | tail -1)
[ -n "$KEY" ] || fail "could not create an API key"
"$PY" -m app.ingestion.cli DATA/true_data --tenant "$TENANT" --source-type true >/dev/null || fail "ingestion of DATA/true_data failed (Gemini key valid? Qdrant reachable?)"
export LIVE_TENANT="$TENANT" LIVE_API_KEY="$KEY"
"$PY" -m pytest tests/live -m live -s -q -p no:cacheprovider -W ignore >live_pytest.log 2>&1 || { tail -5 live_pytest.log >&2; fail "live tests failed (see live_pytest.log)"; }
"$PY" scripts/make_live_report.py live_results.json live_report.md || fail "could not build live_report.md"
echo "OK: live_report.md written"
