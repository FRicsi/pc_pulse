# Run once in an elevated PowerShell, after editing config.json.
$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$admin) { throw 'Run PowerShell as Administrator.' }
$config = Get-Content (Join-Path $PSScriptRoot '..\config.json') -Raw | ConvertFrom-Json
Write-Host 'Audit enablement process started. Large drives may take a long time.'
# Locale independent File System audit subcategory.
& auditpol.exe /set '/subcategory:{0CCE921D-69AE-11D9-BED3-505054503030}' /success:enable
if ($LASTEXITCODE -ne 0) { throw 'auditpol failed.' }
$sid = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
$rights = [Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::Delete
if ($config.reads) { $rights = $rights -bor [Security.AccessControl.FileSystemRights]::ReadData }
$rule = New-Object Security.AccessControl.FileSystemAuditRule($sid, $rights, 'ContainerInherit,ObjectInherit', 'None', 'Success')
foreach ($raw in $config.watch) {
  $path = [Environment]::ExpandEnvironmentVariables($raw)
  if (!(Test-Path -LiteralPath $path -PathType Container)) { Write-Warning "Missing folder: $path"; continue }
  Write-Host "Audit enabling ongoing on: $path -- please wait."
  $timer = [Diagnostics.Stopwatch]::StartNew()
  $acl = Get-Acl -LiteralPath $path -Audit
  $acl.AddAuditRule($rule)
  Set-Acl -LiteralPath $path -AclObject $acl
  $timer.Stop()
  Write-Host ('Audit enabled: {0} -- elapsed: {1:n0} seconds' -f $path, $timer.Elapsed.TotalSeconds)
}
Write-Host 'Done. Existing protected subfolders may not inherit this rule. Restart PC Pulse if config changed.'
