# Release checklist

Do these in order. Every script exits non-zero with a one-line reason on failure and is safe to re-run.
Run from the repository root. Commands are given for **bash** (Linux/macOS/Git Bash) and **PowerShell** (Windows).

## 1. Create `.env`
```bash
cp .env.example .env
```
```powershell
Copy-Item .env.example .env
```
Edit `.env` and fill at least `GROQ_API_KEY`, `PORTKEY_API_KEY`, `GEMINI_API_KEY`. For the nightly evals also set `RAG_EVAL_API_URL`, `RAG_EVAL_API_KEY`, `JUDGE_GROQ`
(they are only used as GitHub secrets, step 7). `.env` is git-ignored. Never paste key values into chat, issues or commit messages.

Python environment (once): `uv venv --python 3.11 .venv && uv pip install --python .venv -r requirements-dev.lock`
(Windows: the same commands; the venv python is `.venv\Scripts\python.exe`).

## 2. Verify against the real model providers  -> `live_report.md`
```bash
bash scripts/verify_live.sh
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1
```
It checks the three keys exist (without printing them), starts/uses Postgres, Redis and Qdrant (from `.env`, otherwise `docker compose up -d --wait postgres redis qdrant`),
runs migrations, creates a test tenant + key, ingests `DATA/true_data`, then runs `pytest tests/live -m live` with the hard budget (150 LLM calls, 500 embeddings).
Read `live_report.md`: guardrail false-block / false-pass rates, Portkey fallback result, groundedness spot checks, latency per stage.
Pass criteria used by the test: <= 2 true-data questions wrongly blocked and <= 2 jailbreak prompts answered (out of 20 / 10). Tune `RERANK_MIN_SCORE`, `ASSISTANT_DOMAIN` or the rail prompts if not met.

## 3. Verify the container stack  -> `docker_report.md`
```bash
bash scripts/verify_docker.sh
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_docker.ps1
```
Builds the image, brings up postgres/pgbouncer/redis/qdrant/api/worker, waits for the healthchecks, checks migrations, that the API runs as the non-root `app` user, key CLI,
ingestion, `/query`, `/readyz`, that `/metrics` is not on the public port and 9100 is not published, scans the image with trivy (local binary, otherwise `aquasec/trivy` via docker),
then runs `docker compose down -v`. With no API keys in `.env` it uses fake models (`APP_ENV=loadtest`) and says so in the report.

## 4. Attribution check
```bash
bash scripts/check_attribution.sh
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\check_attribution.ps1
```
Must print `check_attribution OK`. (A local-only `commit-msg` hook in `.git/hooks` rejects assistant attribution in new commits.)

## 5. Push to a private GitHub repository
```bash
gh auth status                                  # log in first if needed: gh auth login
gh repo create YOUR-NAME/prod-rag --private --source . --remote origin --push
```
```powershell
gh auth status
gh repo create YOUR-NAME/prod-rag --private --source . --remote origin --push
```
Without `gh`: create an empty private repo in the GitHub UI, then `git remote add origin git@github.com:YOUR-NAME/prod-rag.git && git push -u origin HEAD`.

## 6. Confirm CI is green
```bash
gh run list --limit 5
gh run watch                                    # pick the latest "ci" run; all four jobs must pass:
                                                # lint-and-security, tests-offline, tests-integration, image
```
If `image` fails on Trivy, read the job log: fix by upgrading the base image / the flagged package and push again.

## 7. Add repository secrets for the nightly evals
```bash
for k in GROQ_API_KEY PORTKEY_API_KEY GEMINI_API_KEY JUDGE_GROQ RAG_EVAL_API_URL RAG_EVAL_API_KEY; do
  v=$(grep -E "^$k=" .env | head -1 | cut -d= -f2- | sed -E 's/^["'\'' ]+|["'\'' ]+$//g')
  [ -n "$v" ] && printf '%s' "$v" | gh secret set "$k" || echo "skipped $k (empty in .env)"
done
```
```powershell
foreach ($k in "GROQ_API_KEY","PORTKEY_API_KEY","GEMINI_API_KEY","JUDGE_GROQ","RAG_EVAL_API_URL","RAG_EVAL_API_KEY") {
  $line = Get-Content .env | Where-Object { $_ -match "^$k=" } | Select-Object -First 1
  $v = if ($line) { ($line -replace "^[^=]*=","").Trim().Trim('"').Trim("'") } else { "" }
  if ($v) { $v | gh secret set $k } else { Write-Host "skipped $k (empty in .env)" }
}
```
`RAG_EVAL_API_URL` is the base URL of a deployed API (the workflow appends `/query`); `RAG_EVAL_API_KEY` must be an `eval`-scope key
(`python -m app.auth.cli create --tenant T --user ci --scope eval`). The nightly workflow skips cleanly if any secret is missing.
Trigger it once to check: `gh workflow run nightly-evals.yml` then `gh run watch`.

## After release
* `kubectl apply -k deploy/k8s/base` after creating the `rag-secrets` Secret (see `deploy/k8s/base/secret.example.yaml`); run `python -m app.ingestion.cli ... --rebuild` once per tenant.
* Re-measure capacity on your hardware (`DOCS/12_CAPACITY.md`) before trusting its per-pod figure.
