param(
    [string]$TaskName = "GateKeptDailyReport",
    [string]$ScriptPath = "$PSScriptRoot\daily_report_cron.sh",
    [string]$WorkingDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path,
    [string]$StartTime = "00:00",
    [string]$User = "$env:USERNAME"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ScriptPath)) {
    throw "Report script not found: $ScriptPath"
}

$taskExists = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($taskExists) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File \"$ScriptPath\"" -WorkingDirectory $WorkingDirectory
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

Write-Host "Scheduled task '$TaskName' created to run daily at $StartTime."
Write-Host "Working directory: $WorkingDirectory"
Write-Host "Script: $ScriptPath"
