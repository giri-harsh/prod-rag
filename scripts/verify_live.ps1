# Verify the system against REAL Groq / Portkey / Gemini.   powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1
# Needs .env with GROQ_API_KEY and GEMINI_API_KEY (values are never printed). A non-empty PORTKEY_API_KEY switches the run to gateway mode
# (USE_GATEWAY=true + the Portkey fallback test); otherwise Groq is called directly (USE_GATEWAY=false, fallback test SKIPPED-NO-PORTKEY).
# Idempotent; exits non-zero with one line on failure.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
function Fail($msg) { [Console]::Error.WriteLine("verify_live FAILED: $msg"); exit 1 }

if (-not (Test-Path .env)) { Fail ".env not found - copy .env.example to .env and fill the three API keys" }
function EnvVal($k) {
    $line = Get-Content .env | Where-Object { $_ -match "^\s*$k\s*=" } | Select-Object -Last 1
    if (-not $line) { return "" }
    $v = ($line -replace "^[^=]*=", "") -replace "\s+#.*$", ""
    return $v.Trim().Trim('"').Trim("'")
}
foreach ($k in "GROQ_API_KEY", "GEMINI_API_KEY") { if (-not (EnvVal $k)) { Fail "$k is empty in .env" } }
if ((EnvVal "PORTKEY_API_KEY") -or $env:PORTKEY_API_KEY) { $env:USE_GATEWAY = "true"; $mode = "gateway" } else { $env:USE_GATEWAY = "false"; $mode = "direct-groq" }
Write-Host "mode: $mode (USE_GATEWAY=$($env:USE_GATEWAY))"

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } elseif ($env:PYTHON) { $env:PYTHON } else { "python" }
& $py -c "import app.config" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "cannot import the app with $py (create the venv: uv venv --python 3.11 .venv; uv pip install -r requirements-dev.lock)" }

$haveInfra = ($env:DATABASE_URL -or (EnvVal "DATABASE_URL")) -and ($env:REDIS_URL -or (EnvVal "REDIS_URL")) -and ($env:QDRANT_URL -or (EnvVal "QDRANT_URL"))
if (-not $haveInfra) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "DATABASE_URL/REDIS_URL/QDRANT_URL are not set in .env and docker is not available to start them" }
    docker compose up -d --wait postgres redis qdrant *> $null
    if ($LASTEXITCODE -ne 0) { Fail "could not start postgres/redis/qdrant with docker compose" }
    $env:DATABASE_URL = "postgresql://rag:rag@127.0.0.1:5432/rag"; $env:REDIS_URL = "redis://127.0.0.1:6379/0"; $env:QDRANT_URL = "http://127.0.0.1:6333"
}

$env:LIVE_REPORT_JSON = "live_results.json"
Remove-Item -ErrorAction SilentlyContinue live_results.json, live_report.md
$tenant = "verifylive"
& $py -m app.db.migrate *> $null
if ($LASTEXITCODE -ne 0) { Fail "migrations failed" }
$key = (& $py -m app.auth.cli create --tenant $tenant --user tester --scope eval --label verify_live 2>$null | Select-Object -Last 1)
if (-not $key) { Fail "could not create an API key" }
& $py -m app.auth.cli tenant-set --tenant $tenant --domain "Kubernetes (architecture, Jobs, CronJobs, autoscaling, work queues), container workloads and Databricks job automation" *> $null
if ($LASTEXITCODE -ne 0) { Fail "could not set the tenant domain" }
& $py -m app.ingestion.cli DATA/true_data --tenant $tenant --source-type true *> $null
if ($LASTEXITCODE -ne 0) { Fail "ingestion of DATA/true_data failed (Gemini key valid? Qdrant reachable?)" }
$env:LIVE_TENANT = $tenant; $env:LIVE_API_KEY = $key
& $py -m pytest tests/live -m live -s -q -p no:cacheprovider -W ignore *> live_pytest.log
if ($LASTEXITCODE -ne 0) { Get-Content live_pytest.log -Tail 5 | ForEach-Object { [Console]::Error.WriteLine($_) }; Fail "live tests failed (see live_pytest.log)" }
& $py scripts/make_live_report.py live_results.json live_report.md
if ($LASTEXITCODE -ne 0) { Fail "could not build live_report.md" }
Write-Host "OK: live_report.md written"
