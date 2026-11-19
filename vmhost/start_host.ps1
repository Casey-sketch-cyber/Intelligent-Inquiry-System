$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Project must be on D:' }
Set-Location -LiteralPath $root
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PYTHONIOENCODING = 'utf-8'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
New-Item -ItemType Directory -Path "$root\data\logs",$env:TEMP -Force | Out-Null
Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList '-B','-m','vmhost.host' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\data\logs\vm-host-$stamp.log" -RedirectStandardError "$root\data\logs\vm-host-$stamp-error.log"
