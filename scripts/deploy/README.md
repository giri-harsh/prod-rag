# Deploying to one VM (Oracle Cloud A1, Ubuntu 24.04)

Stack on the VM: caddy (80/443, Let's Encrypt) -> web (BFF + SPA) -> api -> pgbouncer -> postgres, plus worker, redis, qdrant.
Only caddy publishes host ports. Code is shipped with `git archive` + `scp`; GitHub is not involved.

## 0. One-time prerequisites (laptop)
- Windows OpenSSH client: `Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0` (admin PowerShell).
- Everything you want deployed is **committed** (`git archive HEAD` ships committed files only).
- `.env` holds real values for `GROQ_API_KEY`, `GEMINI_API_KEY`, `POSTGRES_PASSWORD`, `QDRANT_API_KEY`, `SITE_DOMAIN=rag.harshgiri.site`
  (optionally `GROQ_FALLBACK_API_KEY`). Passwords must be URL-safe: `openssl rand -hex 24`. Leave `BFF_RAG_API_KEY` empty; the VM fills it in.

## 1. Create the VM (Oracle console)
1. Compute -> Instances -> Create: image **Canonical Ubuntu 24.04** (aarch64), shape **VM.Standard.A1.Flex**, 4 OCPU / 24 GB, add your SSH public key.
2. Networking -> your VCN -> Subnet -> **Security List** -> Add Ingress Rules: source `0.0.0.0/0`, TCP, destination ports `80` and `443` (22 is there by default).
3. Note the VM's **public IP**.

## 2. DNS (Namecheap)
Domain List -> `harshgiri.site` -> Advanced DNS -> Add New Record: **A Record**, Host `rag`, Value `<VM public IP>`, TTL Automatic.
Check with `nslookup rag.harshgiri.site` before the first HTTPS request (Let's Encrypt needs it).

## 3. Ship and bootstrap (laptop, PowerShell, repo root)
```powershell
.\scripts\deploy\push_to_vm.ps1 -VmHost <VM_IP> -KeyPath C:\path\to\oracle.key
```
Uploads the code tar, `.env` and `DATA/true_data`, then runs `bootstrap_vm.sh` on the VM: installs Docker Engine + compose plugin,
opens ufw 22/80/443, fixes Oracle's iptables REJECT rule (persisted), creates a 4G swapfile, builds the images,
starts both compose files, waits for `api` to be healthy, installs the nightly backup cron. Safe to re-run.

## 4. Verify and create the tenant (VM)
```bash
ssh -i <key> ubuntu@<VM_IP> 'bash ~/prod_rag/scripts/deploy/verify_on_vm.sh'
```
Creates tenant `demo` (domain "Kubernetes and networking") with admin/user/eval keys (kept in `~/prod_rag/.keys.env`, mode 600),
sets the user key as `BFF_RAG_API_KEY` and restarts web, ingests `DATA/true_data`, queries through `https://rag.harshgiri.site`,
runs `verify_live`, a 3-user/2-minute locust run and prints `=== REPORT FROM VM ===` (image sizes, stats, 429 count).
Groq free tier is 30 RPM / 8K TPM per model: 429s under load are expected.

Then open <https://rag.harshgiri.site>.

## Updating
```powershell
git commit ...                      # commit first
.\scripts\deploy\push_to_vm.ps1 -VmHost <VM_IP> -KeyPath <key> -SkipBootstrap
ssh -i <key> ubuntu@<VM_IP> 'bash ~/prod_rag/scripts/deploy/update.sh'
```
The VM's `.env` is kept unless you pass `-ReplaceEnv`.

## Backups
`update.sh`/`bootstrap_vm.sh` install a 03:15 cron job: `pg_dump -Fc` via the postgres container into `~/prod_rag/backups/` (14 newest kept).
(`scripts/backup_restore.sh` needs a reachable DB URL; postgres is not published, so the cron uses `docker compose exec`.)
Restore: `docker compose ... exec -T postgres pg_restore -U rag -d rag --clean --if-exists < backups/rag_YYYY-MM-DD.dump`.
Qdrant data lives in the `qdrantdata` volume and can be rebuilt by re-ingesting.

## Troubleshooting
- Site unreachable: Oracle Security List ports (step 1.2), DNS (step 2), `sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml logs caddy`.
- `web` restarting right after the first deploy is normal until `verify_on_vm.sh` sets `BFF_RAG_API_KEY`.
- Container exit 137 = OOM: raise that service's `mem_limit` in `docker-compose.prod.yml` (api max 6g) or set `WEB_CONCURRENCY=1`.
- Docker bypasses ufw for published ports, which is why only caddy publishes any.
