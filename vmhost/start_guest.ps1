$ErrorActionPreference = 'Stop'
$root = 'D:\TianxinRuntime'
Set-Location -LiteralPath $root
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
& "$root\.venv\Scripts\python.exe" -B -m vmhost.guest >> "$root\data\logs\guest-service.log" 2>&1
