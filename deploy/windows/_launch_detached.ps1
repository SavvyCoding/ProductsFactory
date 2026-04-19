# Helper: launches start_poller.ps1 in a detached, hidden PowerShell process.
# Used once by the deploy flow to bring the poller back up after a pm-api
# rebuild. For permanent persistence across reboots, use install_task.ps1.

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$StartScript = Join-Path $RepoRoot "deploy\windows\start_poller.ps1"

if (-not (Test-Path $StartScript)) {
    Write-Error "start_poller.ps1 not found at $StartScript"
    exit 1
}

# Redirect the hidden PowerShell's own stderr (for PowerShell parse errors or
# unhandled script exceptions that never make it to poller.log) to a boot log.
$BootLog = Join-Path $RepoRoot "orchestrator\poller_boot.log"

$proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $StartScript) `
    -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden `
    -RedirectStandardError $BootLog `
    -PassThru

if ($proc) {
    Write-Host "Poller launched (detached). PID=$($proc.Id). stderr -> $BootLog"
} else {
    Write-Error "Start-Process returned no process object - WDAC likely blocked the spawn"
    exit 1
}
