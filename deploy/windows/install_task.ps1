# ProductFactory - Register poller as a Windows Scheduled Task
# Run this script ONCE as Administrator.
#
# Usage:  powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
#
# The task starts at system boot, runs hidden, and auto-restarts on crash.

$TaskName  = "ProductFactoryPoller"
$RepoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Script    = Join-Path $RepoRoot "deploy\windows\start_poller.ps1"
$LogFile   = Join-Path $RepoRoot "orchestrator\poller.log"

# Verify script exists
if (-not (Test-Path $Script)) {
    Write-Error "Script not found: $Script"
    exit 1
}

# Remove existing task if present (idempotent)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action: run start_poller.ps1 hidden
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $RepoRoot

# Trigger: run at boot, delay 60s to let Docker containers start first
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Trigger.Delay = "PT60S"

# Run as current user (logged-in sessions only; sufficient for a dev machine)
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# ExecutionTimeLimit 0 = no time limit (poller runs forever)
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "ProductFactory autonomous dev poller" `
    -Force

Write-Host ""
Write-Host "Task registered: $TaskName" -ForegroundColor Green
Write-Host ""
Write-Host "Start now (no reboot needed):"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Check status:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State"
Write-Host ""
Write-Host "View logs:"
Write-Host "  Get-Content $LogFile -Tail 50 -Wait"
