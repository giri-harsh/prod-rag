# Build and exercise the real container stack.   powershell -ExecutionPolicy Bypass -File scripts\verify_docker.ps1
# With the three API keys in .env it runs against real Groq/Portkey/Gemini; without them it runs the SAME containers with APP_ENV=loadtest
# (fake LLM/embeddings, real Postgres/Redis/Qdrant) and says so in docker_report.md. Idempotent; always tears the stack down.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
$report = "docker_report.md"
function Fail($msg) { [Console]::Error.WriteLine("verify_docker FAILED: $msg"); Add-Content $report "- **FAILED:** $msg"; docker compose down -v *> $null; exit 1 }
function Note($msg) { Add-Content $report $msg }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { [Console]::Error.WriteLine("verify_docker FAILED: docker not found"); exit 1 }
docker info *> $null
if ($LASTEXITCODE -ne 0) { [Console]::Error.WriteLine("verify_docker FAILED: docker daemon not running"); exit 1 }

function EnvVal($k) {
    if (-not (Test-Path .env)) { return "" }
    $line = Get-Content .env | Where-Object { $_ -match "^\s*$k\s*=" } | Select-Object -Last 1
    if (-not $line) { return "" }
    return (($line -replace "^[^=]*=", "") -replace "\s+#.*$", "").Trim().Trim('"').Trim("'")
}
$fake = $false
foreach ($k in "GROQ_API_KEY", "GEMINI_API_KEY") { if (-not (EnvVal $k)) { $fake = $true } }
if ($fake) { $env:APP_ENV = "loadtest"; $mode = "FAKE models (APP_ENV=loadtest; GROQ/GEMINI keys missing in .env)" }
else {
    $env:APP_ENV = "dev"
    if (EnvVal "PORTKEY_API_KEY") { $env:USE_GATEWAY = "true"; $mode = "REAL models (keys found in .env), Portkey gateway" } else { $env:USE_GATEWAY = "false"; $mode = "REAL models (keys found in .env), direct Groq (no PORTKEY_API_KEY)" }
}
Set-Content $report "# Docker verification report`n`n- mode: $mode`n- date: $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))`n"

docker compose down -v *> $null
docker compose up -d --build *> docker_build.log
if ($LASTEXITCODE -ne 0) { Get-Content docker_build.log -Tail 5 | ForEach-Object { [Console]::Error.WriteLine($_) }; Fail "docker compose up --build failed (see docker_build.log)" }

function IsHealthy($svc) {
    $id = (docker compose ps -q $svc) | Select-Object -First 1
    if (-not $id) { return $false }
    return ((docker inspect --format '{{.State.Health.Status}}' $id) -eq "healthy")
}
foreach ($svc in "postgres", "redis", "qdrant", "pgbouncer", "api") {
    $ok = $false
    for ($i = 0; $i -lt 240; $i++) { if (IsHealthy $svc) { $ok = $true; break }; Start-Sleep 1 }
    if (-not $ok) { Fail "timed out waiting for $svc to be healthy" }
}
$mig = docker inspect --format '{{.State.ExitCode}}' ((docker compose ps -aq migrate) | Select-Object -First 1)
if ($mig -ne "0") { Fail "migrate job exited with code $mig" }
Note "- compose stack healthy (postgres, redis, qdrant, pgbouncer, api); migrations exit code 0"

$who = (docker compose exec -T api whoami).Trim()
if ($who -ne "app") { Fail "api container runs as '$who', expected non-root user 'app'" }
Note "- api container user: ``$who`` (non-root) - OK"

$key = (docker compose exec -T api python -m app.auth.cli create --tenant dockerverify --user tester --scope eval 2>$null | Select-Object -Last 1)
if (-not $key) { Fail "could not create an API key in the container" } else { $key = $key.Trim() }
docker compose exec -T api python -m app.ingestion.cli /data/true_data --tenant dockerverify --source-type true *> docker_ingest.log
if ($LASTEXITCODE -ne 0) { Get-Content docker_ingest.log -Tail 3 | ForEach-Object { [Console]::Error.WriteLine($_) }; Fail "ingestion inside the container failed (see docker_ingest.log)" }
Note "- key CLI and ingestion of /data/true_data inside the container - OK"

try {
    $r = Invoke-WebRequest -UseBasicParsing -Method Post -Uri http://localhost:8080/query -Headers @{ "X-API-Key" = $key } -ContentType "application/json" `
        -Body '{"q":"How does the horizontal pod autoscaler work?","session_id":"dv1"}'
    if ($r.StatusCode -ne 200 -or $r.Content -notmatch '"answer"') { Fail "POST /query returned HTTP $($r.StatusCode) without an answer" }
} catch { Fail "POST /query failed: $($_.Exception.Message)" }
Note "- POST /query -> 200 with an answer - OK"
function Code($url) { try { (Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3).StatusCode } catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } } }
if ((Code "http://localhost:8080/readyz") -ne 200) { Fail "/readyz is not 200" }
if ((Code "http://localhost:8080/metrics") -ne 404) { Fail "/metrics must NOT be served on the public port" }
if ((Code "http://localhost:9100/metrics") -ne 0) { Fail "metrics port 9100 must not be published to the host" }
Note "- /readyz 200; /metrics not on the public port; 9100 not published - OK"

$img = "prod-rag:local"
Note "`n## Vulnerability scan (trivy, HIGH/CRITICAL, fixable only)`n"
if (Get-Command trivy -ErrorAction SilentlyContinue) { trivy image --quiet --severity HIGH, CRITICAL --ignore-unfixed --exit-code 1 $img *> docker_trivy.log }
else { docker run --rm -v //var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest image --quiet --severity HIGH, CRITICAL --ignore-unfixed --exit-code 1 $img *> docker_trivy.log }
if ($LASTEXITCODE -ne 0) { Note '```'; Add-Content $report (Get-Content docker_trivy.log -Tail 40); Note '```'; Fail "trivy found fixable HIGH/CRITICAL vulnerabilities (see docker_report.md)" }
Note "- trivy: no fixable HIGH/CRITICAL findings in ``$img``"
Note "`n**RESULT: PASS**"
docker compose down -v *> $null
Write-Host "OK: docker_report.md written"
