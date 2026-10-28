#!/usr/bin/env bash
# Run ON the VM after bootstrap_vm.sh:   bash ~/prod_rag/scripts/deploy/verify_on_vm.sh
# Creates tenant "demo" + admin/user/eval keys (stored in ~/prod_rag/.keys.env, mode 600; never echoed in full),
# puts the user key in .env as BFF_RAG_API_KEY and restarts web, ingests DATA/true_data, sends one real query through
# https://$SITE_DOMAIN, runs scripts/verify_live.sh in a container, load-tests with locust (3 users, 2 min) and prints a REPORT block.
# Groq free tier is 30 RPM / 8K TPM per model: 429s during the load test are expected and are only counted.
set -uo pipefail
APP_DIR="${APP_DIR:-$HOME/prod_rag}"
cd "$APP_DIR" || { echo "no $APP_DIR" >&2; exit 1; }
DC="sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml"
PROJ=$(basename "$APP_DIR" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')
NET="${PROJ}_default"
OUT="$APP_DIR/verify_out"; mkdir -p "$OUT"; chmod 777 "$OUT"
fail() { echo "verify_on_vm FAILED: $1" >&2; exit 1; }
step() { echo; echo "==> $1"; }
mask() { printf '%s...' "${1:0:6}"; }
env_val() { grep -E "^[[:space:]]*$1[[:space:]]*=" .env | tail -1 | sed -E "s/^[^=]*=//; s/[[:space:]]+#.*$//; s/^[[:space:]\"']+//; s/[[:space:]\"']+$//"; }
set_env() {  # set_env KEY VALUE : replace or append in .env without printing the value
  if grep -qE "^[[:space:]]*$1[[:space:]]*=" .env; then
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > .env.tmp && cat .env.tmp > .env && rm -f .env.tmp
  else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
[ -f .env ] || fail ".env missing"
SITE_DOMAIN=$(env_val SITE_DOMAIN); SITE_DOMAIN=${SITE_DOMAIN:-localhost}
declare -A R

step "Images"
R[api_image]=$(sudo docker image ls prod-rag:local --format '{{.Size}}')
R[web_image]=$(sudo docker image ls prod-rag-web:local --format '{{.Size}}')
R[arch]=$(uname -m)
echo "api ${R[api_image]} | web ${R[web_image]} | arch ${R[arch]} (native build on this VM = the arm64 check)"

step "Readiness + migrate"
R[readyz]=$($DC exec -T api curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/readyz 2>/dev/null || echo fail)
R[migrate]=$($DC ps -a migrate --format '{{.State}} exit={{.ExitCode}}' 2>/dev/null | head -1)
echo "readyz=${R[readyz]} migrate=${R[migrate]}"
[ "${R[readyz]}" = 200 ] || fail "api /readyz is not 200"
$DC logs --tail 3 worker 2>/dev/null | tail -3

step "Tenant + keys"
KEYS_FILE="$APP_DIR/.keys.env"
if [ -f "$KEYS_FILE" ]; then
  echo "reusing keys from .keys.env"
else
  mk() { $DC exec -T api python -m app.auth.cli create "$@" 2>/dev/null | tail -1; }
  ADMIN_KEY=$(mk --tenant demo --user admin --admin --label vm-admin)
  USER_KEY=$(mk --tenant demo --user user --label vm-user)
  EVAL_KEY=$(mk --tenant demo --user eval --scope eval --label vm-eval)
  [ -n "$ADMIN_KEY" ] && [ -n "$USER_KEY" ] && [ -n "$EVAL_KEY" ] || fail "key creation failed (see: $DC logs api)"
  (umask 077; printf 'ADMIN_KEY=%s\nUSER_KEY=%s\nEVAL_KEY=%s\n' "$ADMIN_KEY" "$USER_KEY" "$EVAL_KEY" > "$KEYS_FILE")
fi
# shellcheck disable=SC1090
. "$KEYS_FILE"
echo "admin $(mask "$ADMIN_KEY") user $(mask "$USER_KEY") eval $(mask "$EVAL_KEY")  (full keys: $KEYS_FILE)"
$DC exec -T api python -m app.auth.cli tenant-set --tenant demo --domain "Kubernetes and networking" >/dev/null || fail "tenant-set failed"
if [ "$(env_val BFF_RAG_API_KEY)" != "$USER_KEY" ]; then
  set_env BFF_RAG_API_KEY "$USER_KEY"; chmod 600 .env
  $DC up -d web caddy >/dev/null
fi
for i in $(seq 1 30); do
  [ "$($DC ps web --format '{{.Health}}' 2>/dev/null | head -1)" = healthy ] && break; sleep 2
done
R[web]=$($DC ps web --format '{{.State}}/{{.Health}}' | head -1)
echo "web: ${R[web]}"

step "Ingest DATA/true_data"
if ! $DC exec -T api python -m app.ingestion.cli /data/true_data --tenant demo --source-type true >"$OUT/ingest.log" 2>&1; then
  echo "ingest failed once; retrying with INGEST_BATCH_SIZE=16 (Gemini rate limits)"
  $DC exec -T -e INGEST_BATCH_SIZE=16 api python -m app.ingestion.cli /data/true_data --tenant demo --source-type true >"$OUT/ingest.log" 2>&1 \
    || { tail -5 "$OUT/ingest.log"; R[ingest]=FAIL; }
fi
[ -n "${R[ingest]:-}" ] || R[ingest]=ok
tail -2 "$OUT/ingest.log"

step "Query through https://$SITE_DOMAIN"
body='{"q":"How does the horizontal pod autoscaler decide when to scale pods?","session_id":"vmcheck1"}'
curl_q() { curl -sS --max-time 90 --resolve "$SITE_DOMAIN:443:127.0.0.1" "$@" -H "Origin: https://$SITE_DOMAIN" -H 'Content-Type: application/json' -d "$body" -w '\nHTTP %{http_code}\n' "https://$SITE_DOMAIN/api/query"; }
if [ "$SITE_DOMAIN" = localhost ]; then QOUT=$(curl_q -k 2>&1); else QOUT=$(curl_q 2>&1) || QOUT=$(curl_q -k 2>&1); fi
R[query_http]=$(printf '%s' "$QOUT" | awk '/^HTTP /{print $2}' | tail -1)
printf '%s\n' "$QOUT" | head -c 700; echo
R[tls]=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 --resolve "$SITE_DOMAIN:443:127.0.0.1" "https://$SITE_DOMAIN/healthz" 2>/dev/null && echo trusted || echo untrusted-or-pending)

step "verify_live.sh (in a python:3.11 container on the compose network)"
PW=$(env_val POSTGRES_PASSWORD)
sudo docker run --rm --network "$NET" -v "$APP_DIR:/work" -w /work -e PYTHON=python \
  -e DATABASE_URL="postgresql://rag:$PW@postgres:5432/rag" -e REDIS_URL=redis://redis:6379/0 \
  -e QDRANT_URL=http://qdrant:6333 -e QDRANT_API_KEY="$(env_val QDRANT_API_KEY)" -e APP_ENV=dev \
  python:3.11-slim bash -c 'pip install -q --prefer-binary -r requirements-dev.lock && bash scripts/verify_live.sh' >"$OUT/verify_live.log" 2>&1
R[verify_live_rc]=$?
tail -5 "$OUT/verify_live.log"
sudo chown -R "$USER" live_report.md live_results.json live_pytest.log 2>/dev/null || true
if [ -f live_report.md ]; then grep -iE 'false.?block|false.?pass|failover|fallback' live_report.md | head -8; fi

step "Idle docker stats"
sleep 20
sudo docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' | tee "$OUT/stats_idle.txt"

step "Locust: 3 users, 2 minutes, real models"
echo "{\"demo\": \"$USER_KEY\"}" > "$OUT/load_keys.json"; chmod 644 "$OUT/load_keys.json"
: > "$OUT/stats_peak_samples.txt"
( while [ ! -f "$OUT/.locust_done" ]; do sudo docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' >> "$OUT/stats_peak_samples.txt"; sleep 5; done ) &
SAMPLER=$!
rm -f "$OUT/.locust_done"
sudo docker run --rm --network "$NET" -v "$APP_DIR/loadtest:/mnt/loadtest:ro" -v "$OUT:/mnt/out" -e LOAD_KEYS=/mnt/out/load_keys.json \
  locustio/locust:2.32.4 -f /mnt/loadtest/locustfile.py --headless -u 3 -r 1 -t 2m --host http://api:8080 --csv /mnt/out/locust >"$OUT/locust.log" 2>&1
touch "$OUT/.locust_done"; wait "$SAMPLER" 2>/dev/null
tail -12 "$OUT/locust.log"

# peak per container: max CPU% and max memory (MiB) over all samples
peak=$(awk -F'\t' '
  function mib(s,  n,u){ n=s+0; u=s; gsub(/[0-9.]/,"",u); if(u ~ /^GiB/)return n*1024; if(u ~ /^KiB|^kB/)return n/1024; if(u ~ /^MiB/)return n; return n/1048576 }
  { split($3,m," / "); c=$2+0; mm=mib(m[1]); if(c>cpu[$1])cpu[$1]=c; if(mm>mem[$1])mem[$1]=mm }
  END{ for(k in cpu) printf "%s\tpeak_cpu=%.1f%%\tpeak_mem=%.0fMiB\n", k, cpu[k], mem[k] }' "$OUT/stats_peak_samples.txt" | sort)
c429=$(awk -F, 'NR>1 && /429/ {n+=$NF} END{print n+0}' "$OUT/locust_failures.csv" 2>/dev/null)
agg=$(awk -F, '$2=="Aggregated"{printf "reqs=%s fails=%s p50=%sms p95=%sms rps=%.2f", $3,$4,$5,$17,$10}' "$OUT/locust_stats.csv" 2>/dev/null)

step "Host"
free -h; df -h / | tail -2

echo
echo "=== VM REPORT FOR BRAIN ==="
echo "arch: ${R[arch]}   (native build here => arm64 check PASS if arch=aarch64)"
echo "image sizes: api ${R[api_image]} | web ${R[web_image]}"
echo "readyz: ${R[readyz]}   migrate: ${R[migrate]}   web: ${R[web]}   tls: ${R[tls]}"
echo "ingest: ${R[ingest]}   query via https://$SITE_DOMAIN: HTTP ${R[query_http]:-none}"
echo "verify_live exit code: ${R[verify_live_rc]}  (log: $OUT/verify_live.log, report: live_report.md)"
echo "locust (3 users, 2m): ${agg:-no stats}   429 failures: ${c429:-0}"
echo "-- idle (CPU, RAM)"; cat "$OUT/stats_idle.txt"
echo "-- peak"; echo "$peak"
echo "-- host"; free -h | awk '/Mem:|Swap:/'; df -h / | tail -1
echo "Copy this block (and $OUT/*.log tails) back to the brain. Keys are in $KEYS_FILE - do not paste them."
