<#
.SYNOPSIS
  Ship the committed code (git archive HEAD), the .env and DATA/true_data to the VM over scp, then run bootstrap_vm.sh there.
  No GitHub involved. Never prints .env contents.
.EXAMPLE
  .\scripts\deploy\push_to_vm.ps1 -VmHost 203.0.113.10 -KeyPath $HOME\.ssh\oracle.key
  .\scripts\deploy\push_to_vm.ps1 -VmHost 203.0.113.10 -KeyPath $HOME\.ssh\oracle.key -ReplaceEnv   # also overwrite the VM's .env
#>
param(
  [Parameter(Mandatory = $true)][string]$VmHost,
  [Parameter(Mandatory = $true)][string]$KeyPath,
  [string]$User = 'ubuntu',
  [switch]$ReplaceEnv,      # by default an existing VM .env is kept (it holds BFF_RAG_API_KEY written by verify_on_vm.sh)
  [switch]$SkipBootstrap    # only upload + extract, do not run bootstrap_vm.sh
)
$ErrorActionPreference = 'Stop'
function Die([string]$m) { Write-Host "push_to_vm FAILED: $m" -ForegroundColor Red; exit 1 }

foreach ($tool in 'ssh', 'scp', 'git', 'tar') {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    if ($tool -in 'ssh', 'scp') { Die "$tool not found. Install Windows OpenSSH client (admin PowerShell): Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0" }
    Die "$tool not found on PATH"
  }
}
if (-not (Test-Path $KeyPath)) { Die "key file not found: $KeyPath" }
$repo = (git -C $PSScriptRoot rev-parse --show-toplevel).Trim()
Set-Location $repo
if (-not (Test-Path .env)) { Die ".env not found in $repo (copy .env.example and fill it)" }
if (-not (Test-Path DATA/true_data)) { Die 'DATA/true_data not found' }
if (git status --porcelain --untracked-files=no) { Write-Host 'warning: uncommitted changes are NOT shipped (git archive HEAD); commit first if you want them on the VM' -ForegroundColor Yellow }

$sshOpts = @('-i', $KeyPath, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=15', '-o', 'BatchMode=yes')
$dest = "$User@$VmHost"
& ssh @sshOpts $dest 'true' 2>$null
if ($LASTEXITCODE -ne 0) { Die "cannot ssh to $dest with that key (host down, wrong IP/user/key, or VCN security list blocks port 22)" }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("rag_push_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $tmp | Out-Null
try {
  $code = Join-Path $tmp 'prod_rag.tar'
  $data = Join-Path $tmp 'prod_rag_data.tar'
  git archive --format=tar -o $code HEAD -- . ':(exclude)DATA' ':(exclude)DOCS' ':(exclude)_audit' ':(exclude)notebooks'
  if ($LASTEXITCODE -ne 0) { Die 'git archive failed' }
  tar -cf $data -C DATA true_data
  if ($LASTEXITCODE -ne 0) { Die 'could not tar DATA/true_data' }

  $envPresent = $false
  & ssh @sshOpts $dest 'test -f ~/prod_rag/.env' 2>$null
  if ($LASTEXITCODE -eq 0) { $envPresent = $true }

  $files = @($code, $data, (Join-Path $repo 'scripts/deploy/bootstrap_vm.sh'))
  if ($ReplaceEnv -or -not $envPresent) { $files += (Join-Path $repo '.env') } else { Write-Host 'VM already has a .env - keeping it (use -ReplaceEnv to overwrite)' }
  Write-Host ("uploading {0:N1} MB code + {1:N1} MB data ..." -f ((Get-Item $code).Length / 1MB), ((Get-Item $data).Length / 1MB))
  foreach ($f in $files) {
    $name = Split-Path $f -Leaf
    if ($name -eq '.env') { $name = 'prod_rag.env.new' }
    & scp @sshOpts $f "${dest}:~/$name"
    if ($LASTEXITCODE -ne 0) { Die "scp of $name failed" }
  }
} finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }

if ($SkipBootstrap) { Write-Host 'uploaded; skipped bootstrap'; exit 0 }
& ssh @sshOpts -t $dest 'chmod 600 ~/prod_rag.env.new 2>/dev/null; bash ~/bootstrap_vm.sh'
if ($LASTEXITCODE -ne 0) { Die 'bootstrap_vm.sh failed on the VM (output above)' }
Write-Host "done. Next: ssh -i $KeyPath $dest 'bash ~/prod_rag/scripts/deploy/verify_on_vm.sh'"
