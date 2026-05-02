# Register a Windows Scheduled Task that runs scripts/backup_db_2h.sh
# every 2 hours, on the hour. User-level task — no admin required.
#
# Usage (from repo root):
#   pwsh scripts/setup_backup_schedule.ps1
#
# Idempotent — re-running re-registers with the latest definition.
#
# To remove the task later:
#   Unregister-ScheduledTask -TaskName "ProductFactory-DB-Backup-2h" -Confirm:$false

$ErrorActionPreference = 'Stop'

$TaskName = "ProductFactory-DB-Backup-2h"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$ScriptPath = Join-Path $RepoRoot "scripts\backup_db_2h.sh"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "backup_db_2h.log"

# Find Git Bash. Falls back to a sensible default if `git` isn't on PATH.
$BashCmd = $null
try {
    $gitExe = (Get-Command git -ErrorAction Stop).Source
    $gitDir = Split-Path -Parent $gitExe                  # ...\Git\cmd or ...\Git\bin
    $gitRoot = Split-Path -Parent $gitDir                 # ...\Git
    $BashCmd = Join-Path $gitRoot "bin\bash.exe"
    if (-not (Test-Path $BashCmd)) {
        $BashCmd = Join-Path $gitRoot "usr\bin\bash.exe"
    }
} catch {}
if (-not $BashCmd -or -not (Test-Path $BashCmd)) {
    $BashCmd = "C:\Program Files\Git\bin\bash.exe"
}
if (-not (Test-Path $BashCmd)) {
    Write-Error "Git Bash not found. Install Git for Windows or set `$BashCmd manually in this script."
    exit 1
}
Write-Host "Using bash: $BashCmd"

# Quote the script path for cygwin-style consumption: convert C:\foo\bar.sh
# to /c/foo/bar.sh so bash on Windows treats it as a POSIX path.
$ScriptPathPosix = "/" + ($ScriptPath -replace '\\', '/' -replace '^([A-Za-z]):', { "$($_.Groups[1].Value.ToLower())" })

# Action: bash.exe -lc "exec >> log 2>&1; echo --start--; bash script"
$BashArgs = '-lc "exec >> ''' + ($LogFile -replace '\\', '/') + ''' 2>&1; echo \"--- start $(date -u +%Y-%m-%dT%H:%M:%SZ) ---\"; bash ''' + $ScriptPathPosix + '''"'
$Action = New-ScheduledTaskAction -Execute $BashCmd -Argument $BashArgs -WorkingDirectory $RepoRoot

# Trigger: every 2 hours starting at the next even hour.
$Now = Get-Date
$NextEvenHour = $Now.AddHours(1).Date.AddHours( [int]([math]::Floor($Now.AddHours(1).Hour / 2) * 2) )
if ($NextEvenHour -le $Now) { $NextEvenHour = $NextEvenHour.AddHours(2) }
Write-Host "First run scheduled: $NextEvenHour"
$Trigger = New-ScheduledTaskTrigger -Once -At $NextEvenHour `
    -RepetitionInterval (New-TimeSpan -Hours 2)

# Settings: don't pile up missed runs (overwrite-by-name handles catch-up
# implicitly), allow on battery, allow on demand.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Re-register (idempotent)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "ProductFactory DB backup, every 2 hours, 12-slot rolling overwrite" | Out-Null

Write-Host ""
Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Script:  $ScriptPath"
Write-Host "  Log:     $LogFile"
Write-Host "  Trigger: every 2 hours starting $NextEvenHour"
Write-Host ""
Write-Host "Test now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Inspect:   Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
