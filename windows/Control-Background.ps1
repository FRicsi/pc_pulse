param([ValidateSet('Start','Stop','Restart','Enable','Disable','Status','Uninstall')][string]$Action = 'Status', [switch]$Json)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ($Action -ne 'Status') {
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (!$admin) { throw 'Run this control as Administrator.' }
}
$task = Get-ScheduledTask -TaskName 'PC Pulse' -ErrorAction SilentlyContinue
if (!$task) {
  if ($Action -eq 'Status' -and $Json) {
    @{ installed=$false; state='Missing'; enabled=$false; message='PC Pulse background task is not installed.' } | ConvertTo-Json -Compress
    exit 0
  }
  throw 'PC Pulse background task is not installed.'
}
switch ($Action) {
  'Start' { Start-ScheduledTask -TaskName 'PC Pulse' }
  'Stop' { Stop-ScheduledTask -TaskName 'PC Pulse' }
  'Restart' {
    Stop-ScheduledTask -TaskName 'PC Pulse'
    for ($i=0; $i -lt 40; $i++) {
      if ((Get-ScheduledTask -TaskName 'PC Pulse').State -ne 'Running') { break }
      Start-Sleep -Milliseconds 250
    }
    if ((Get-ScheduledTask -TaskName 'PC Pulse').State -eq 'Running') { throw 'Background task did not stop.' }
    if (!(Get-ScheduledTask -TaskName 'PC Pulse').Settings.Enabled) { throw 'Background task is disabled. Enable it first.' }
    Start-ScheduledTask -TaskName 'PC Pulse'
  }
  'Enable' { Enable-ScheduledTask -TaskName 'PC Pulse' | Out-Null; Start-ScheduledTask -TaskName 'PC Pulse' }
  'Disable' { Disable-ScheduledTask -TaskName 'PC Pulse' | Out-Null; Stop-ScheduledTask -TaskName 'PC Pulse' }
  'Uninstall' {
    Stop-ScheduledTask -TaskName 'PC Pulse'
    Unregister-ScheduledTask -TaskName 'PC Pulse' -Confirm:$false
    if (Get-ScheduledTask -TaskName 'PC Pulse Control' -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName 'PC Pulse Control'
      Unregister-ScheduledTask -TaskName 'PC Pulse Control' -Confirm:$false
    }
    Write-Host 'Tasks removed. Data, config, password and Windows audit settings are retained.'
    exit
  }
}
if ($Json) {
  $task = Get-ScheduledTask -TaskName 'PC Pulse'
  $info = Get-ScheduledTaskInfo -TaskName 'PC Pulse'
  @{ installed=$true; state=[string]$task.State; enabled=[bool]$task.Settings.Enabled;
     lastResult=$info.LastTaskResult; lastRun=$info.LastRunTime.ToUniversalTime().ToString('o');
     source=$task.Actions.Arguments; action=$Action } | ConvertTo-Json -Compress
  exit 0
}
Get-ScheduledTask -TaskName 'PC Pulse' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'PC Pulse' | Select-Object LastRunTime, LastTaskResult, NextRunTime
