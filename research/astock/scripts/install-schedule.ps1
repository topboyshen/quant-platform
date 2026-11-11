[CmdletBinding(SupportsShouldProcess)]
param(
  [ValidatePattern("^([01]\d|2[0-3]):[0-5]\d$")]
  [string]$At = "08:00",
  [ValidatePattern("^[A-Za-z0-9_-]+$")]
  [string]$TaskName = "AStock_AI_Morning_Report",
  [switch]$Remove
)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Remove) {
  if (-not $existing) {
    Write-Host "Scheduled task does not exist: $TaskName"
    exit 0
  }
  if ($PSCmdlet.ShouldProcess($TaskName, "Remove Windows scheduled task")) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName"
  }
  exit 0
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $PSScriptRoot "run-morning-report.ps1"
$actionArgs = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -StrictData -TradingDaysOnly' -f $runner
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $At
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($PSCmdlet.ShouldProcess($TaskName, "Create or update the weekday $At morning-report task")) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Generate the A-share morning report with Pi and Ollama Cloud DeepSeek V4 Flash" -Force | Out-Null
  Write-Host "Scheduled task deployed: $TaskName (weekdays at $At; exchange holidays are skipped after calendar validation; runs while the current user is logged in)."
}
