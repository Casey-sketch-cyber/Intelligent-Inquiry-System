$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Install on D:' }
Set-Location -LiteralPath $root
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PYTHONIOENCODING = 'utf-8'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
New-Item -ItemType Directory -Path "$root\data\logs",$env:TEMP -Force | Out-Null
Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList '-B','-m','lanhost.controller' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\data\logs\lan-office-$stamp.log" -RedirectStandardError "$root\data\logs\lan-office-$stamp-error.log"
