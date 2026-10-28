#!/usr/bin/env bash
# Idempotent VM bootstrap (Ubuntu 24.04, amd64 or arm64). Run on the VM as the normal sudo user (e.g. ubuntu):
#   bash ~/bootstrap_vm.sh
# Expects push_to_vm.ps1 to have uploaded ~/prod_rag.tar, ~/prod_rag_data.tar and (first time) ~/prod_rag.env.new.
# Re-running is safe: Docker/ufw/swap steps are skipped when already done, an existing .env is kept.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/prod_rag}"
DC="sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml"
fail() { echo "bootstrap FAILED: $1" >&2; exit 1; }
step() { echo; echo "==> $1"; }

[ "$(id -u)" -ne 0 ] || fail "run as a normal user with sudo (not root), so files are owned by you"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || echo "warning: tested on Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}"
echo "arch: $(uname -m)  cpus: $(nproc)  mem: $(free -h | awk '/Mem:/{print $2}')"

step "Docker Engine + compose plugin"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "already installed"
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true   # takes effect on next login; this script uses sudo docker
sudo docker compose version

step "Firewall (ufw)"
# Oracle images ship with ufw disabled and rely on iptables (next step). Only touch ufw if it is already active.
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q '^Status: active'; then
  sudo ufw allow 22/tcp >/dev/null
  sudo ufw allow 80/tcp >/dev/null
  sudo ufw allow 443/tcp >/dev/null
  sudo ufw status | head -8
else
  echo "ufw is not active - leaving it disabled (iptables rules below handle 80/443)"
fi

step "Oracle Cloud iptables"
# Oracle's Ubuntu images ship iptables rules ending in REJECT icmp-host-prohibited; insert ACCEPT for 80/443 before it and persist.
if sudo iptables -S INPUT 2>/dev/null | grep -q 'REJECT'; then
  for port in 80 443; do
    if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
      pos=$(sudo iptables -L INPUT --line-numbers -n | awk '/REJECT/{print $1; exit}')
      sudo iptables -I INPUT "${pos:-1}" -p tcp --dport "$port" -m state --state NEW -j ACCEPT
      echo "inserted ACCEPT tcp/$port at INPUT position ${pos:-1}"
    fi
  done
  if ! dpkg -s iptables-persistent >/dev/null 2>&1; then
    echo iptables-persistent iptables-persistent/autosave_v4 boolean true | sudo debconf-set-selections
    echo iptables-persistent iptables-persistent/autosave_v6 boolean true | sudo debconf-set-selections
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
  fi
  sudo netfilter-persistent save
else
  echo "no REJECT rule in INPUT - nothing to fix"
fi
cat <<'EOF'
REMINDER (Oracle Cloud console): the VCN subnet's Security List (or NSG) must ALLOW ingress TCP 22, 80 and 443 from 0.0.0.0/0.
Without that, Let's Encrypt cannot validate and the site is unreachable even though everything here is fine.
EOF

step "Swap"
if [ -z "$(swapon --show --noheadings)" ]; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "4G swapfile created"
else
  echo "swap already present"
fi

step "Unpack code, data, env"
mkdir -p "$APP_DIR"
if [ -f "$HOME/prod_rag.tar" ]; then tar -xf "$HOME/prod_rag.tar" -C "$APP_DIR" && echo "code extracted to $APP_DIR"; fi
if [ -f "$HOME/prod_rag_data.tar" ]; then mkdir -p "$APP_DIR/DATA" && tar -xf "$HOME/prod_rag_data.tar" -C "$APP_DIR/DATA" && echo "DATA/true_data extracted"; fi
if [ -f "$HOME/prod_rag.env.new" ]; then mv -f "$HOME/prod_rag.env.new" "$APP_DIR/.env"; echo ".env installed"; fi
chmod 600 "$APP_DIR/.env" 2>/dev/null || true
cd "$APP_DIR"
[ -f docker-compose.prod.yml ] || fail "docker-compose.prod.yml not found in $APP_DIR (code tar missing? run push_to_vm.ps1)"
[ -f .env ] || fail ".env missing in $APP_DIR. Create it from .env.example (or re-run push_to_vm.ps1 -ReplaceEnv)"
[ -d DATA/true_data ] || echo "warning: DATA/true_data missing - ingestion in verify_on_vm.sh will fail"

env_val() { grep -E "^[[:space:]]*$1[[:space:]]*=" .env | tail -1 | sed -E "s/^[^=]*=//; s/[[:space:]]+#.*$//; s/^[[:space:]\"']+//; s/[[:space:]\"']+$//"; }
missing=""
for k in GROQ_API_KEY GEMINI_API_KEY POSTGRES_PASSWORD QDRANT_API_KEY SITE_DOMAIN; do
  [ -n "$(env_val $k)" ] || missing="$missing $k"
done
[ -z "$missing" ] || fail ".env is missing values for:$missing (values are not printed)"
printf '%s' "$(env_val POSTGRES_PASSWORD)" | grep -Eq '^[A-Za-z0-9_.~-]+$' || fail "POSTGRES_PASSWORD must be URL-safe (letters, digits, _ . ~ -); generate with: openssl rand -hex 24"

step "Build and start"
$DC config -q || fail "compose config invalid"
$DC up -d --build
# migrate is a one-shot service the api waits for (it uses DIRECT_DATABASE_URL, not pgbouncer); make a failure explicit
mig=$($DC ps -a migrate --format '{{.State}} exit={{.ExitCode}}' | head -1)
echo "migrate: $mig"
case "$mig" in *"exit=0"*) ;; *) $DC logs --tail 30 migrate; fail "migrate did not finish cleanly" ;; esac

step "Waiting for api to be healthy (up to 10 min)"
for i in $(seq 1 120); do
  cid=$($DC ps -q api || true)
  st=$([ -n "$cid" ] && sudo docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" || echo none)
  [ "$st" = healthy ] && break
  sleep 5
done
[ "$st" = healthy ] || { $DC ps; $DC logs --tail 40 api migrate; fail "api not healthy (status: $st)"; }

step "Nightly backup cron"
bash scripts/deploy/update.sh --cron-only

step "Health"
$DC ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
$DC exec -T api curl -fsS http://127.0.0.1:8080/readyz && echo
echo
echo "OK. Next: bash $APP_DIR/scripts/deploy/verify_on_vm.sh   (creates the tenant + keys, ingests, load-tests, prints the report)"
echo "Point DNS A record for $(env_val SITE_DOMAIN) to this VM's public IP if you have not (see scripts/deploy/README.md)."
