param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$admin) { throw 'Run Install-Background.cmd as Administrator.' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dest = Join-Path $env:ProgramFiles 'PC-Pulse'
if (!$PythonPath) {
  $candidates = @()
  try { $candidates += (& py -3 -c 'import sys; print(sys.executable)').Trim() } catch {}
  $candidates += @(Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Python*\python.exe') -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
  $PythonPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (!$PythonPath) { throw 'Python 3.10+ was not found. Supply -PythonPath with the path to python.exe.' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
& $PythonPath -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
$base = (& $PythonPath -I -c 'import sys; print(sys.base_prefix)').Trim()
if ($LASTEXITCODE -ne 0 -or !(Test-Path (Join-Path $base 'Lib'))) { throw 'A standard CPython installation with Lib is required.' }
# Reject reparse points in the interpreter and destination ancestry.
foreach ($target in @($PythonPath, $dest)) {
  $part = $target
  while ($part) {
    if (Test-Path -LiteralPath $part) {
      $item = Get-Item -LiteralPath $part -Force
      if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse path not supported: $part" }
    }
    $parent = Split-Path $part -Parent
    if ($parent -eq $part) { break }; $part = $parent
  }
}
$old = Get-ScheduledTask -TaskName 'PC Pulse' -ErrorAction SilentlyContinue
if ($old) { Stop-ScheduledTask -TaskName 'PC Pulse'; Start-Sleep -Seconds 2 }
$oldControl = Get-ScheduledTask -TaskName 'PC Pulse Control' -ErrorAction SilentlyContinue
if ($oldControl -and $oldControl.State -eq 'Running') {
  Stop-ScheduledTask -TaskName 'PC Pulse Control'
  for ($i=0; $i -lt 40; $i++) {
    if ((Get-ScheduledTask -TaskName 'PC Pulse Control').State -ne 'Running') { break }
    Start-Sleep -Milliseconds 250
  }
  if ((Get-ScheduledTask -TaskName 'PC Pulse Control').State -eq 'Running') { throw 'Control task did not stop.' }
}
New-Item -ItemType Directory -Path $dest -Force | Out-Null
# Protect executable scripts and config before registering an elevated task.
& icacls.exe $dest /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q
if ($LASTEXITCODE -ne 0) { throw 'Cannot secure install folder.' }
# Repair child ACLs left protected by pre-0.3.2 recursive inheritance removal.
Get-ChildItem -LiteralPath $dest -Force | ForEach-Object {
  & icacls.exe $_.FullName /reset /T /Q
  if ($LASTEXITCODE -ne 0) { throw ('Cannot restore inherited permissions: ' + $_.FullName) }
}
if ($source -ne $dest) {
  Copy-Item -LiteralPath (Join-Path $source 'server.py') -Destination $dest -Force
  Copy-Item -LiteralPath (Join-Path $source 'PC-Pulse.html') -Destination $dest -Force
  Copy-Item -LiteralPath (Join-Path $source 'control.py') -Destination $dest -Force
  foreach ($file in @('Start-Background.cmd','Stop-Background.cmd','Restart-Background.cmd','Enable-Autostart.cmd','Disable-Autostart.cmd','Background-Status.cmd')) { Copy-Item -LiteralPath (Join-Path $source $file) -Destination $dest -Force }
  New-Item -ItemType Directory -Path (Join-Path $dest 'windows') -Force | Out-Null
  Copy-Item -Path (Join-Path $source 'windows\*.ps1') -Destination (Join-Path $dest 'windows') -Force
}
$destConfig = Join-Path $dest 'config.json'
if ($source -ne $dest -or !(Test-Path -LiteralPath $destConfig)) {
  if (Test-Path -LiteralPath $destConfig) { Copy-Item -LiteralPath $destConfig -Destination ($destConfig + '.bak') -Force }
  $config = Get-Content -LiteralPath (Join-Path $source 'config.json') -Raw | ConvertFrom-Json
  # Resolve the installing user's variables now, before switching to SYSTEM.
  $config.watch = @($config.watch | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) })
  $config.exclude = @($config.exclude | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) })
  $config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $destConfig -Encoding UTF8
}
# Preserve an existing local history when installing for the first time.
if ($source -ne $dest -and !(Test-Path (Join-Path $dest 'data')) -and (Test-Path (Join-Path $source 'data'))) {
  Copy-Item -LiteralPath (Join-Path $source 'data') -Destination $dest -Recurse
}
& icacls.exe $dest /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q
if ($LASTEXITCODE -ne 0) { throw 'Cannot secure installed files.' }
# Make a private stdlib-only runtime. The SYSTEM task never executes from a user's Python folder.
$runtime = Join-Path $dest 'runtime'
$runtimePython = Join-Path $runtime 'python.exe'
if ($PythonPath -ne $runtimePython) {
  if (Test-Path -LiteralPath $runtime) { Remove-Item -LiteralPath $runtime -Recurse -Force }
  New-Item -ItemType Directory -Path $runtime -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $base 'python.exe') -Destination $runtime -Force
  Copy-Item -Path (Join-Path $base '*.dll') -Destination $runtime -Force
  foreach ($folder in @('Lib', 'DLLs')) {
    $from = Join-Path $base $folder
    if (Test-Path -LiteralPath $from) {
      & robocopy.exe $from (Join-Path $runtime $folder) /E /XJ /XD site-packages __pycache__ /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
      if ($LASTEXITCODE -ge 8) { throw "Runtime copy failed: $folder" }
    }
  }
}
& icacls.exe $runtime /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect private Python runtime.' }
& $runtimePython -I -c 'import sqlite3, ctypes, http.server, subprocess, xml.etree.ElementTree'
if ($LASTEXITCODE -ne 0) { throw 'Private runtime self-test failed.' }
# Everyone SID on watched roots also applies to future Windows accounts via inheritance.
& (Join-Path $dest 'windows\Enable-Audit.ps1')
$action = New-ScheduledTaskAction -Execute $runtimePython -Argument ('-I "' + (Join-Path $dest 'server.py') + '"') -WorkingDirectory $dest
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'PC Pulse' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'PC Pulse'
& (Join-Path $dest 'windows\Install-Control.ps1')
Write-Host "Installed. Config: $destConfig"
Write-Host 'Starts at Windows startup, even before logon. Dashboard: http://127.0.0.1:8765 (or configured port).'
