# Register a daily Windows Task Scheduler task that runs scripts/backup_db.sh.
#
# Created in response to the 2026-04-19 data-loss incident: scripts/seed.py
# was run against a populated DB and TRUNCATEd everything. `scripts/backup_db.sh`
# already existed but was never scheduled, so no recovery point existed.
#
# Default schedule: every day at 03:00 local time.
#     -At <datetime> to override (e.g. "3am" / "04:30" / "2026-04-20T02:00:00")
#
# This script registers the task under the current user (not SYSTEM) because
# backup_db.sh needs access to the repo's .env file + docker CLI. No admin
# privilege required.
#
# Usage:
#     powershell -File deploy\windows\install_backup_task.ps1
#     powershell -File deploy\windows\install_backup_task.ps1 -At "04:30"
#     powershell -File deploy\windows\install_backup_task.ps1 -Uninstall

[CmdletBinding()]
param(
    [string] $At = "03:00",
    [switch] $Uninstall,
    [string] $TaskName = "ProductFactoryBackup"
)

$ErrorActionPreference = "Stop"
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BackupScript = Join-Path $RepoRoot "scripts\backup_db.sh"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered task: $TaskName"
    } else {
        Write-Host "No task named $TaskName is registered — nothing to do."
    }
    return
}

if (-not (Test-Path $BackupScript)) {
    Write-Error "backup_db.sh not found at $BackupScript"
    exit 1
}

# Locate Git Bash. backup_db.sh is a bash script so we can't run it via cmd.
$BashCandidates = @(
    "C:\Program Files\Git\bin\bash.exe",
    "C:\Program Files (x86)\Git\bin\bash.exe",
    "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
)
$Bash = $BashCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Bash) {
    Write-Error ("Git Bash not found. Tried: " + ($BashCandidates -join ', '))
    exit 1
}

# Convert the repo path to the POSIX form Git Bash expects (/c/Users/... not C:\Users\...).
# bash.exe accepts both, but the -c script uses $REPO_ROOT internally so this
# is really just about the working directory.
$action = New-ScheduledTaskAction `
    -Execute $Bash `
    -Argument "-lc `"bash '$($BackupScript.Replace('\','/'))'`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# Run whether user is logged on or not, with stored credentials. Highest
# privileges aren't needed (user owns the repo + has Docker CLI access).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$description = "ProductFactory daily PostgreSQL backup (scripts/backup_db.sh). Keeps 7 rotating dumps in backups/db/. Created by deploy/windows/install_backup_task.ps1."

# Replace an existing registration so re-running this script is idempotent.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing $TaskName registration"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description $description | Out-Null

Write-Host "Registered scheduled task '$TaskName' — runs daily at $At"
Write-Host ""
Write-Host "Verify:    Get-ScheduledTask -TaskName $TaskName"
Write-Host "Run now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Last run:  Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime,LastTaskResult"
Write-Host "Uninstall: powershell -File deploy\windows\install_backup_task.ps1 -Uninstall"
