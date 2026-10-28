#!/usr/bin/env bash
# Redeploy on the VM after a new code tarball was uploaded (from the laptop run:
#   .\scripts\deploy\push_to_vm.ps1 -VmHost <ip> -KeyPath <key> -SkipBootstrap
# then on the VM:  bash ~/prod_rag/scripts/deploy/update.sh).
# Extracts ~/prod_rag.tar over the app dir (the VM's .env, DATA and volumes are untouched), rebuilds and restarts.
# Also installs the nightly database backup cron entry (idempotent).
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/prod_rag}"
cd "$APP_DIR"
. scripts/deploy/compose_env.sh   # sets DC (adds docker-compose.micro.yml when DEPLOY_PROFILE=micro)
if [ "${1:-}" != --cron-only ]; then
  if [ -f "$HOME/prod_rag.tar" ]; then
    tar -xf "$HOME/prod_rag.tar" -C "$APP_DIR"
    mv -f "$HOME/prod_rag.tar" "$HOME/prod_rag.tar.applied"
    echo "new code extracted"
  else
    echo "no ~/prod_rag.tar uploaded - rebuilding the code already on the VM"
  fi
  [ -f .env ] || { echo "update FAILED: .env missing" >&2; exit 1; }
  $DC up -d --build --remove-orphans
  sudo docker image prune -f >/dev/null
fi

# nightly logical backup at 03:15 into ~/prod_rag/backups (keeps 14). Uses the session-mode URL, not the pooler.
CRON_LINE="15 3 * * * cd $APP_DIR && $DC exec -T postgres sh -c 'pg_dump -U rag -Fc rag' > backups/rag_\$(date +\%F).dump 2>>backups/backup.log && ls -1t backups/*.dump | tail -n +15 | xargs -r rm -f"
mkdir -p backups
if ! crontab -l 2>/dev/null | grep -qF "backups/rag_"; then
  (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
  echo "cron entry installed"
fi
[ "${1:-}" != --cron-only ] || exit 0
sleep 5
$DC ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
# /readyz can answer 503 for a few seconds right after the api restarts (guardrails and pools warming); wait for it instead of failing
for i in $(seq 1 24); do
  if $DC exec -T api curl -fsS http://127.0.0.1:8080/readyz >/dev/null 2>&1; then echo "readyz: ready"; exit 0; fi
  sleep 5
done
echo "update FAILED: /readyz is still not ready after 2 minutes" >&2
$DC logs --tail 30 api >&2
exit 1
