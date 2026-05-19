# ProductFactory Poller - startup script for Windows Task Scheduler
# Loads .env, then runs the poller. Restarts automatically if it crashes.
#
# To install as a scheduled task: run deploy\windows\install_task.ps1 as Administrator
# To run manually: powershell -File deploy\windows\start_poller.ps1
#
# DEPRECATED 2026-05-18: the live orchestrator now runs inside the
# pf-orchestrator container — `docker compose --profile orchestrator up -d`.
# This wrapper is preserved as a break-glass fallback only. Refuses to start
# unless ALLOW_LEGACY_POLLER=1 is set, to prevent silent crash-loops like the
# 2026-05-15 PyJWT incident.

if (-not $env:ALLOW_LEGACY_POLLER) {
    Write-Error @"

start_poller.ps1: DEPRECATED — this host-mode wrapper is no longer the live path.
  Live orchestrator:  docker compose --profile orchestrator up -d
  See orchestrator/INVARIANTS.md preface for the consolidation note.
  Override (not recommended):  `$env:ALLOW_LEGACY_POLLER='1'; .\start_poller.ps1
"@
    exit 2
}

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EnvFile  = Join-Path $RepoRoot ".env"
$LogFile  = Join-Path $RepoRoot "orchestrator\poller.log"
# Pick a runnable Python. Order: .venv first (uv-managed, has every requirement
# already installed, and WDAC lets it through when launched via the
# Start-Process Hidden trust chain that _launch_detached.ps1 provides), then
# fall back to user-installed system Pythons.
$PythonCandidates = @(
    (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)
$Python = $null
foreach ($cand in $PythonCandidates) {
    if (-not (Test-Path $cand)) { continue }
    # Strong probe - capture BOTH stdout+stderr and check that we actually got
    # a "Python X.Y.Z" line back. Must be wrapped in try/catch because WDAC
    # raises ApplicationFailedException which, combined with the script-wide
    # $ErrorActionPreference = "Stop", would otherwise abort the whole script
    # on the first blocked candidate.
    try {
        $probe = & $cand --version 2>&1 | Out-String
        if ($probe -match 'Python\s+\d+\.\d+\.\d+') {
            $Python = $cand
            break
        }
    } catch {
        Write-Host "Candidate $cand blocked or unrunnable: $_"
        continue
    }
}
if (-not $Python) {
    Write-Error ("No runnable python.exe found. Tried: " + ($PythonCandidates -join ', '))
    exit 1
}
Write-Host "Using python: $Python"

# Ensure UTF-8 output so Unicode in log messages don't crash on Windows
$env:PYTHONIOENCODING = "utf-8"

# Ensure claude CLI is on PATH (installed by npm -g, lives in user's .local/bin)
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

# -- Load .env into the current process environment ---------------------------
if (-not (Test-Path $EnvFile)) {
    Write-Error ".env not found at $EnvFile - copy .env.example and fill in values"
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

# -- Restart loop - Task Scheduler already handles startup, this handles crashes --
Set-Location $RepoRoot

# -- Wrapper mutex: prevent two concurrent start_poller.ps1 trees --
# _launch_detached.ps1 invocations and Task Scheduler triggers can both spawn
# this script. Without this guard, each spawned wrapper enters its own restart
# loop and they fight for the DB lock indefinitely.
$WrapperPidFile = Join-Path $RepoRoot "orchestrator\wrapper.pid"
if (Test-Path $WrapperPidFile) {
    try {
        $existingPid = [int](Get-Content $WrapperPidFile -ErrorAction Stop).Trim()
        if ($existingPid -ne $PID -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
            Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Wrapper already running (pid=$existingPid). This wrapper exiting."
            exit 0
        }
    } catch {
        # Corrupt file - overwrite below
    }
}
Set-Content -Path $WrapperPidFile -Value $PID
# Best-effort cleanup on script exit (PowerShell doesn't have a true atexit;
# trap fires on uncaught exceptions and Ctrl+C; the loop's exit 0 path also
# falls through here when invoked).
trap {
    try {
        if ((Test-Path $WrapperPidFile) -and ((Get-Content $WrapperPidFile -ErrorAction SilentlyContinue).Trim() -eq "$PID")) {
            Remove-Item $WrapperPidFile -Force -ErrorAction SilentlyContinue
        }
    } catch { }
    continue
}

$StderrLog = Join-Path $RepoRoot "orchestrator\poller_stderr.log"

while ($true) {
    Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Starting poller..."
    try {
        # Use Start-Process -Wait instead of `& | Tee-Object`. Tee-Object in
        # PowerShell 5.1 appears to terminate long-running child processes
        # when stdout is sparse (the poller sleeps POLL_INTERVAL between
        # cycles). Start-Process with direct file redirection has none of
        # that behaviour. `-u` on python gives unbuffered stdout so log
        # lines land in the file immediately.
        #
        # Note: Start-Process requires stdout and stderr to go to DIFFERENT
        # files. The poller's own RotatingFileHandler writes to $LogFile
        # directly, so the child-stdout redirect here is just the
        # print()s that happen before logging is configured.
        $p = Start-Process -FilePath $Python `
            -ArgumentList @('-u','-m','orchestrator.poller') `
            -WorkingDirectory $RepoRoot `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $StderrLog `
            -RedirectStandardError  $StderrLog.Replace('.log', '.err.log')
        $exitCode = if ($p) { $p.ExitCode } else { -1 }
        Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Poller exited with code=$exitCode"
        # Exit code 42 = another instance already running on this machine - do not restart
        if ($exitCode -eq 42) {
            Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Another poller instance is running - this wrapper exiting."
            try {
                if ((Test-Path $WrapperPidFile) -and ((Get-Content $WrapperPidFile -ErrorAction SilentlyContinue).Trim() -eq "$PID")) {
                    Remove-Item $WrapperPidFile -Force -ErrorAction SilentlyContinue
                }
            } catch { }
            exit 0
        }
    } catch {
        Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Poller crashed in launcher: $_"
    }
    Add-Content -Path $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Restarting in 10s..."
    Start-Sleep -Seconds 10
}
