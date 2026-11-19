param([string]$WorkerRoot = 'D:\TianxinWorker')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($WorkerRoot).TrimEnd('\')
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Worker must be on D:' }
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PSModuleAnalysisCachePath = "$root\.cache\powershell\ModuleAnalysisCache"
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator +
    (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
Set-Location -LiteralPath $root
$python = "$root\.venv\Scripts\python.exe"
$permit = Join-Path $PSScriptRoot 'live-permit.json'
$reset = Join-Path $PSScriptRoot 'reset-request.json'
$trial = Join-Path $PSScriptRoot 'live_trial.py'
& $python -B $trial --root $root --permit $permit --reset-request $reset --check-only
if ($LASTEXITCODE -ne 0) { throw 'Live trial permit is invalid; nothing was cleared or sent' }
& (Join-Path $PSScriptRoot 'reset_history.ps1') -WorkerRoot $root -RequestPath $reset
Write-Output 'Starting the authorized live inquiry trial on this dedicated QQ PC.'
Write-Output 'Keep this PC for QQ only. To stop, click Pause on its worker page or press Ctrl+C here.'
& $python -B $trial --root $root --permit $permit
if ($LASTEXITCODE -ne 0) { throw 'Live trial stopped. Inspect saved results; do not rerun or resend automatically.' }
Write-Output 'Trial finished. Office workbench has the three new tasks. Regular sending remains locked.'
