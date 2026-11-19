param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Install on D:' }
Set-Location -LiteralPath $root
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
New-Item -ItemType Directory -Path "$root\data\logs",$env:TEMP -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$arguments = @('-B','-m','lanhost.worker')
if ($NoBrowser) { $arguments += '--no-browser' }
Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\data\logs\lan-worker-$stamp.log" -RedirectStandardError "$root\data\logs\lan-worker-$stamp-error.log"
