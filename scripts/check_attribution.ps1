# Fails (non-zero) if any commit mentions an AI assistant in author/committer/message, or if assistant config files are tracked.
#   powershell -ExecutionPolicy Bypass -File scripts\check_attribution.ps1
Set-Location (Join-Path $PSScriptRoot "..")
$status = 0
$robot = [char]::ConvertFromUtf32(0x1F916)
$log = git log --all --format='%an %ae %cn %ce %B' | Out-String
if ($log -match "(?i)claude|anthropic|$robot") {
    [Console]::Error.WriteLine("check_attribution FAILED: commit history mentions claude/anthropic/robot-emoji in author, committer or message")
    git log --all --format='%h %an <%ae> | %s' | Select-String -Pattern "(?i)claude|anthropic|$robot" | Select-Object -First 5 | ForEach-Object { [Console]::Error.WriteLine($_.Line) }
    $status = 1
}
if (git ls-files | Select-String -Pattern '^(\.claude/|CLAUDE\.md$)') {
    [Console]::Error.WriteLine("check_attribution FAILED: .claude/ or CLAUDE.md is tracked (git rm -r --cached it and add it to .gitignore)")
    $status = 1
}
if ($status -eq 0) { Write-Host "check_attribution OK: history and tracked files are clean" }
exit $status
