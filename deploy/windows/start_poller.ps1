# ProductFactory Poller — startup script for Windows Task Scheduler
# Loads .env, then runs the poller. Restarts automatically if it crashes.
#
# To install as a scheduled task: run deploy\windows\install_task.ps1 as Administrator
# To run manually: powershell -File deploy\windows\start_poller.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EnvFile  = Join-Path $RepoRoot ".env"
$LogFile  = Join-Path $RepoRoot "orchestrator\poller.log"
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# Ensure UTF-8 output so Unicode in log messages don't crash on Windows
$env:PYTHONIOENCODING = "utf-8"

# Ensure claude CLI is on PATH (installed by npm -g, lives in user's .local/bin)
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

# ── Load .env into the current process environment ───────────────────────────
if (-not (Test-Path $EnvFile)) {
    Write-Error ".env not found at $EnvFile — copy .env.example and fill in values"
    exit 1
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $parts = $line.Split("=", 2)
        if ($parts.Length -eq 2) {
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
        }
    }
}

# ── Restart loop — Task Scheduler already handles startup, this handles crashes ──
Set-Location $RepoRoot

while ($true) {
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Starting poller..." | Tee-Object -Append $LogFile
    try {
        & $Python -m orchestrator.poller 2>&1 | Tee-Object -Append $LogFile
    } catch {
        Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Poller crashed: $_" | Tee-Object -Append $LogFile
    }
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Poller exited — restarting in 10s..." | Tee-Object -Append $LogFile
    Start-Sleep -Seconds 10
}
