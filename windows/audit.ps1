param([long]$After = 0, [int]$Limit = 2000)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
  if ($After -eq 0) {
    $e = Get-WinEvent -LogName Security -MaxEvents 1
    @{ cursor = $e.RecordId; events = @(); reset = $false } | ConvertTo-Json -Depth 5 -Compress
    exit 0
  }
  $latest = Get-WinEvent -LogName Security -MaxEvents 1
  if ($latest.RecordId -lt $After) {
    @{ cursor = $latest.RecordId; events = @(); reset = $true } | ConvertTo-Json -Compress
    exit 0
  }
  $rows = @()
  try {
    $rows = @(Get-WinEvent -LogName Security -FilterXPath "*[System[(EventID=4663) and (EventRecordID > $After)]]" -Oldest -MaxEvents $Limit -ErrorAction Stop)
  } catch {
    if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw }
  }
  $out = @($rows | ForEach-Object {
    [xml]$xml = $_.ToXml(); $d = @{}
    foreach ($item in $xml.Event.EventData.Data) { $d[$item.Name] = $item.InnerText }
    @{
      id = $_.RecordId; time = $_.TimeCreated.ToUniversalTime().ToString('o')
      user = "$($d.SubjectDomainName)\$($d.SubjectUserName)"
      program = $d.ProcessName; path = $d.ObjectName; mask = $d.AccessMask; type = $d.ObjectType
    }
  })
  $cursor = $latest.RecordId
  if ($rows.Count -ge $Limit) { $cursor = $rows[-1].RecordId }
  elseif ($rows.Count -gt 0) { $cursor = [Math]::Max($latest.RecordId, $rows[-1].RecordId) }
  @{ cursor = $cursor; events = $out; reset = $false } | ConvertTo-Json -Depth 5 -Compress
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
