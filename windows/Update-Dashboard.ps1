$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$admin) { throw 'Run Update-Dashboard.cmd as Administrator.' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dest = Join-Path $env:ProgramFiles 'PC-Pulse'
if ($source -eq $dest) { throw 'Extract the update outside the installed folder first.' }
$task = Get-ScheduledTask -TaskName 'PC Pulse' -ErrorAction Stop
$running = $task.State -eq 'Running'
if ($running) {
  Stop-ScheduledTask -TaskName 'PC Pulse'
  for ($i=0; $i -lt 20; $i++) {
    if ((Get-ScheduledTask -TaskName 'PC Pulse').State -ne 'Running') { break }
    Start-Sleep -Milliseconds 500
  }
  if ((Get-ScheduledTask -TaskName 'PC Pulse').State -eq 'Running') { throw 'Background task did not stop.' }
}
try {
  foreach ($file in @('server.py', 'PC-Pulse.html')) {
    $target = Join-Path $dest $file
    Copy-Item -LiteralPath $target -Destination ($target + '.bak') -Force
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $target -Force
  }
  Write-Host 'Dashboard updated. Config, history, Python runtime and audit settings retained.'
} catch {
  foreach ($file in @('server.py', 'PC-Pulse.html')) {
    $target = Join-Path $dest $file
    if (Test-Path ($target + '.bak')) { Copy-Item -LiteralPath ($target + '.bak') -Destination $target -Force }
  }
  throw
} finally {
  if ($running) { Start-ScheduledTask -TaskName 'PC Pulse' }
}
