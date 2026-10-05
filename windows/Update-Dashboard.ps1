$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$admin) { throw 'Run Update-Dashboard.cmd as Administrator.' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dest = Join-Path $env:ProgramFiles 'PC-Pulse'
if ($source -eq $dest) { throw 'Extract the update outside the installed folder first.' }
$files = @('server.py','PC-Pulse.html','control.py','Start-Background.cmd','Stop-Background.cmd','Restart-Background.cmd','Enable-Autostart.cmd','Disable-Autostart.cmd','Background-Status.cmd','windows\Control-Background.ps1','windows\Install-Control.ps1')
$task = Get-ScheduledTask -TaskName 'PC Pulse' -ErrorAction Stop
$control = Get-ScheduledTask -TaskName 'PC Pulse Control' -ErrorAction SilentlyContinue
$running = $task.State -eq 'Running'
$controlRunning = $control -and $control.State -eq 'Running'
$previous = @{}
$copied = @()
# Check all source files and reject reparse targets before stopping anything.
foreach ($file in $files) {
  if (!(Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) { throw "Missing update file: $file" }
  $target = Join-Path $dest $file
  $previous[$file] = Test-Path -LiteralPath $target
  $part = $target
  while ($part) {
    if ((Test-Path -LiteralPath $part) -and ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Reparse path not supported: $part" }
    $parent = Split-Path $part -Parent
    if ($parent -eq $part) { break }
    $part = $parent
  }
}
try {
  foreach ($name in @('PC Pulse','PC Pulse Control')) {
    $entry = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($entry -and $entry.State -eq 'Running') {
      Stop-ScheduledTask -TaskName $name
      for ($i=0; $i -lt 40; $i++) {
        if ((Get-ScheduledTask -TaskName $name).State -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
      }
      if ((Get-ScheduledTask -TaskName $name).State -eq 'Running') { throw "Task did not stop: $name" }
    }
  }
  foreach ($file in $files) {
    $target = Join-Path $dest $file
    if ($previous[$file]) { Copy-Item -LiteralPath $target -Destination ($target + '.bak') -Force }
    $copied += $file
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $target -Force
  }
  & (Join-Path $dest 'windows\Install-Control.ps1')
  Write-Host 'Dashboard and persistent local control updated. Config, history, password and audit settings retained.'
  Write-Host 'Settings remain available at the control port (default http://127.0.0.1:8766).'
} catch {
  if (!$control -and (Get-ScheduledTask -TaskName 'PC Pulse Control' -ErrorAction SilentlyContinue)) {
    Stop-ScheduledTask -TaskName 'PC Pulse Control'
    Unregister-ScheduledTask -TaskName 'PC Pulse Control' -Confirm:$false
  }
  foreach ($file in $copied) {
    $target = Join-Path $dest $file
    if ($previous[$file]) {
      Copy-Item -LiteralPath ($target + '.bak') -Destination $target -Force
    } elseif (Test-Path -LiteralPath $target) {
      # Only these explicit new update files may be removed, never a directory tree.
      $absolute = [IO.Path]::GetFullPath($target)
      $boundary = [IO.Path]::GetFullPath($dest).TrimEnd('\') + '\'
      if (!$absolute.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) { throw 'Rollback path is outside the install folder.' }
      Remove-Item -LiteralPath $absolute -Force
    }
  }
  throw
} finally {
  if ($running) { Start-ScheduledTask -TaskName 'PC Pulse' }
  if ($controlRunning -and (Get-ScheduledTask -TaskName 'PC Pulse Control').State -ne 'Running') { Start-ScheduledTask -TaskName 'PC Pulse Control' }
}
