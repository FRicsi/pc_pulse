$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$admin) { throw 'Run Update-Dashboard.cmd as Administrator to install the control task.' }
$dest = Join-Path $env:ProgramFiles 'PC-Pulse'
$python = Join-Path $dest 'runtime\python.exe'
$script = Join-Path $dest 'control.py'
foreach ($target in @($python, $script)) {
  if (!(Test-Path -LiteralPath $target)) { throw "Missing control service file: $target" }
  $part = $target
  while ($part) {
    if ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse path not supported: $part" }
    $parent = Split-Path $part -Parent
    if ($parent -eq $part) { break }
    $part = $parent
  }
}
$existing = Get-ScheduledTask -TaskName 'PC Pulse Control' -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') {
  Stop-ScheduledTask -TaskName 'PC Pulse Control'
  for ($i=0; $i -lt 40; $i++) {
    if ((Get-ScheduledTask -TaskName 'PC Pulse Control').State -ne 'Running') { break }
    Start-Sleep -Milliseconds 250
  }
  if ((Get-ScheduledTask -TaskName 'PC Pulse Control').State -eq 'Running') { throw 'Control task did not stop.' }
}
$action = New-ScheduledTaskAction -Execute $python -Argument ('-I "' + $script + '"') -WorkingDirectory $dest
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'PC Pulse Control' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'PC Pulse Control'
Write-Host 'Local control service installed. Monitoring and audit settings were not changed.'
