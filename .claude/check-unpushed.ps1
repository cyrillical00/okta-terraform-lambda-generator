#Requires -Version 5
# Stop-hook helper. Blocks Claude Code from ending its turn when this repo
# has commits that have not been pushed to its upstream branch.
#
# Lives in .claude/ so it travels with the repo. Anchored to $PSScriptRoot
# so the check always targets THIS repo regardless of Claude's current cwd.
# Exits silently when the repo has no upstream, no commits ahead, or git
# isn't on PATH; only emits blocking output when there's something to push.

$ErrorActionPreference = 'SilentlyContinue'

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $repoRoot)) { exit 0 }
Set-Location -LiteralPath $repoRoot

$upstream = & git rev-parse --abbrev-ref '@{u}' 2>$null
if (-not $upstream) { exit 0 }

$rawCount = & git rev-list --count '@{u}..HEAD' 2>$null
if (-not $rawCount) { exit 0 }

$count = 0
[int]::TryParse(($rawCount | Out-String).Trim(), [ref]$count) | Out-Null
if ($count -le 0) { exit 0 }

$branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
if ($branch) { $branch = $branch.Trim() } else { $branch = 'HEAD' }

$reason = "Blocked: $count unpushed commit(s) in TF Tool. Run ``git push origin $branch`` before ending the turn."
$payload = @{
    decision = 'block'
    reason   = $reason
} | ConvertTo-Json -Compress
Write-Output $payload
exit 0
