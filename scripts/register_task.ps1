<#
.SYNOPSIS
  Register / remove the "AnthropicWatchQuota" scheduled task.

.DESCRIPTION
  Wraps schtasks.exe (works for the current user without elevation, unlike
  Register-ScheduledTask which requires admin in some contexts). The task
  runs scripts/anthropic_quota_watch.py via pythonw.exe (windowless) every
  N minutes while the user is logged on.

  Register:    pwsh -File scripts\register_task.ps1
  Remove:      pwsh -File scripts\register_task.ps1 -Unregister
  Custom gap:  pwsh -File scripts\register_task.ps1 -IntervalMinutes 10
  Run now:     schtasks /Run /TN AnthropicWatchQuota
#>
param(
    [int]$IntervalMinutes = 15,
    [switch]$Unregister
)

$taskName = "AnthropicWatchQuota"

if ($Unregister) {
    schtasks /Delete /TN $taskName /F
    return
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$watcher   = Join-Path $scriptDir "anthropic_quota_watch.py"

# Python launcher precedence: $env:AW_PYTHON > AW_PYTHON in
# aw_config.local.env > pythonw/python auto-detected on PATH.
$pythonw = $env:AW_PYTHON
$localCfg = Join-Path $scriptDir "aw_config.local.env"
if (-not $pythonw -and (Test-Path $localCfg)) {
    $line = Select-String -Path $localCfg -Pattern '^\s*AW_PYTHON\s*=' -EA SilentlyContinue |
            Select-Object -First 1
    if ($line) { $pythonw = ($line.Line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    $pythonw = (Get-Command pythonw.exe -EA SilentlyContinue).Source
}
if (-not $pythonw) { $pythonw = (Get-Command python.exe -EA SilentlyContinue).Source }
if (-not $pythonw) { $pythonw = "pythonw.exe" }

$run = '"{0}" "{1}"' -f $pythonw, $watcher

schtasks /Create /TN $taskName /TR $run /SC MINUTE /MO $IntervalMinutes /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "Registered '$taskName' (every $IntervalMinutes min, windowless)."
    Write-Host "Run immediately:  schtasks /Run /TN $taskName"
}
