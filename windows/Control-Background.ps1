param([ValidateSet('Start','Stop','Enable','Disable','Status','Uninstall')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
if ($Action -ne 'Status') {
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (!$admin) { throw 'Run this control as Administrator.' }
}
switch ($Action) {
  'Start' { Start-ScheduledTask -TaskName 'PC Pulse' }
  'Stop' { Stop-ScheduledTask -TaskName 'PC Pulse' }
  'Enable' { Enable-ScheduledTask -TaskName 'PC Pulse' | Out-Null; Start-ScheduledTask -TaskName 'PC Pulse' }
  'Disable' { Disable-ScheduledTask -TaskName 'PC Pulse' | Out-Null; Stop-ScheduledTask -TaskName 'PC Pulse' }
  'Uninstall' { Stop-ScheduledTask -TaskName 'PC Pulse'; Unregister-ScheduledTask -TaskName 'PC Pulse' -Confirm:$false; Write-Host 'Task removed. Data and Windows audit settings are retained.'; exit }
}
Get-ScheduledTask -TaskName 'PC Pulse' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'PC Pulse' | Select-Object LastRunTime, LastTaskResult, NextRunTime
