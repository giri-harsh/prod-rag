#!/usr/bin/env bash
# Build and exercise the real container stack.  Usage: bash scripts/verify_docker.sh
# With the three API keys in .env it runs against real Groq/Portkey/Gemini; without them it runs the SAME containers with APP_ENV=loadtest
# (fake LLM/embeddings, real Postgres/Redis/Qdrant) and says so in docker_report.md. Idempotent; always tears the stack down.
set -uo pipefail
cd "$(dirname "$0")/.."
REPORT=docker_report.md
fail() { echo "verify_docker FAILED: $1" >&2; echo "- **FAILED:** $1" >> "$REPORT"; exit 1; }

command -v docker >/dev/null 2>&1 || { echo "verify_docker FAILED: docker not found" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "verify_docker FAILED: docker daemon not running" >&2; exit 1; }
cleanup() { docker compose down -v >/dev/null 2>&1; }
trap cleanup EXIT

env_val() { [ -f .env ] && grep -E "^[[:space:]]*$1[[:space:]]*=" .env | tail -1 | sed -E "s/^[^=]*=//; s/[[:space:]]+#.*$//; s/^[[:space:]\"']+//; s/[[:space:]\"']+$//"; }
MODE="REAL models (keys found in .env)"
for k in GROQ_API_KEY GEMINI_API_KEY; do [ -n "$(env_val $k)" ] || MODE="FAKE models (APP_ENV=loadtest; GROQ/GEMINI keys missing in .env)"; done
case "$MODE" in
  FAKE*) export APP_ENV=loadtest ;;
  *) export APP_ENV=dev
     if [ -n "$(env_val PORTKEY_API_KEY)" ]; then export USE_GATEWAY=true; MODE="$MODE, Portkey gateway"; else export USE_GATEWAY=false; MODE="$MODE, direct Groq (no PORTKEY_API_KEY)"; fi ;;
esac
{ echo "# Docker verification report"; echo; echo "- mode: $MODE"; echo "- date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo; } > "$REPORT"

cleanup
docker compose up -d --build >docker_build.log 2>&1 || { tail -5 docker_build.log >&2; fail "docker compose up --build failed (see docker_build.log)"; }

wait_for() {  # wait_for <description> <timeout_s> <command...>
  local d=$1 t=$2; shift 2
  for _ in $(seq 1 "$t"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "timed out waiting for $d"
}
health() { [ "$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q "$1")" 2>/dev/null)" = "healthy" ]; }
for svc in postgres redis qdrant pgbouncer api; do wait_for "$svc to be healthy" 240 health "$svc"; done
MIG=$(docker inspect --format '{{.State.ExitCode}}' "$(docker compose ps -aq migrate)" 2>/dev/null)
[ "$MIG" = "0" ] || fail "migrate job exited with code $MIG"
echo "- compose stack healthy (postgres, redis, qdrant, pgbouncer, api); migrations exit code 0" >> "$REPORT"

WHO=$(docker compose exec -T api whoami 2>/dev/null | tr -d '\r')
[ "$WHO" = "app" ] || fail "api container runs as '$WHO', expected non-root user 'app'"
echo "- api container user: \`$WHO\` (non-root) - OK" >> "$REPORT"

KEY=$(docker compose exec -T api python -m app.auth.cli create --tenant dockerverify --user tester --scope eval 2>/dev/null | tr -d '\r' | tail -1)
[ -n "$KEY" ] || fail "could not create an API key in the container"
docker compose exec -T api python -m app.ingestion.cli /data/true_data --tenant dockerverify --source-type true >docker_ingest.log 2>&1 || { tail -3 docker_ingest.log >&2; fail "ingestion inside the container failed (see docker_ingest.log)"; }
echo "- key CLI and ingestion of /data/true_data inside the container - OK" >> "$REPORT"

CODE=$(curl -s -o docker_query.json -w '%{http_code}' -X POST localhost:8080/query -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"q":"How does the horizontal pod autoscaler work?","session_id":"dv1"}')
[ "$CODE" = "200" ] || fail "POST /query returned HTTP $CODE (see docker_query.json)"
grep -q '"answer"' docker_query.json || fail "/query response has no answer"
echo "- POST /query -> 200 with an answer - OK" >> "$REPORT"
[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:8080/readyz)" = "200" ] || fail "/readyz is not 200"
[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:8080/metrics)" = "404" ] || fail "/metrics must NOT be served on the public port"
curl -s -m 2 -o /dev/null localhost:9100/metrics && fail "metrics port 9100 must not be published to the host"
echo "- /readyz 200; /metrics not on the public port; 9100 not published - OK" >> "$REPORT"

IMG=$(docker compose images api --format json 2>/dev/null | tr -d '\n' | sed -E 's/.*"Repository":"([^"]+)".*"Tag":"([^"]+)".*/\1:\2/')
[ -n "$IMG" ] || IMG=prod-rag:local
echo >> "$REPORT"; echo "## Vulnerability scan (trivy, HIGH/CRITICAL, fixable only)" >> "$REPORT"; echo >> "$REPORT"
if command -v trivy >/dev/null 2>&1; then
  TRIVY=(trivy image --quiet --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$IMG")
else
  TRIVY=(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest image --quiet --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$IMG")
fi
if "${TRIVY[@]}" >docker_trivy.log 2>&1; then echo "- trivy: no fixable HIGH/CRITICAL findings in \`$IMG\`" >> "$REPORT"
else echo '```' >> "$REPORT"; tail -40 docker_trivy.log >> "$REPORT"; echo '```' >> "$REPORT"; fail "trivy found fixable HIGH/CRITICAL vulnerabilities (see docker_report.md)"; fi

echo >> "$REPORT"; echo "**RESULT: PASS**" >> "$REPORT"
echo "OK: docker_report.md written"
