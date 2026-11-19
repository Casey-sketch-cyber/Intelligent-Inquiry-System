param([Parameter(Mandatory=$true)][string]$JobId)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase) -or $JobId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid update target' }
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PSModuleAnalysisCachePath = "$root\.cache\powershell\ModuleAnalysisCache"
$env:PYTHONIOENCODING = 'utf-8'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
Set-Location -LiteralPath $root
$python = "$root\.venv\Scripts\python.exe"
$tool = "$root\lanhost\support_update.py"
$stopped = $false
try {
    $stateJson = & $python -B $tool --root $root --job-id $JobId --phase prepare
    if ($LASTEXITCODE -ne 0) { throw 'Update preparation failed' }
    $state = $stateJson | ConvertFrom-Json
    $owners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort ([int]$state.ui_port) -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 1) { throw 'Worker listener is ambiguous' }
    $workerProcessId = [int]$owners[0]
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$workerProcessId"
    if (-not $process.ExecutablePath -or @($state.process_executables) -inotcontains $process.ExecutablePath -or $process.CommandLine -notmatch '(?i)(?:^|\s)-m\s+lanhost\.worker(?:\s|$)') { throw 'Worker identity mismatch' }
    Stop-Process -Id $workerProcessId -ErrorAction Stop
    $stopped = $true
    Wait-Process -Id $workerProcessId -ErrorAction SilentlyContinue
    & $python -B $tool --root $root --job-id $JobId --phase install
    if ($LASTEXITCODE -ne 0) { throw 'Update installation failed' }
} catch {
    & $python -B $tool --root $root --job-id $JobId --phase failed
    throw
} finally {
    if ($stopped) { & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$root\lanhost\start_worker.ps1" -NoBrowser }
}
for ($attempt=0; $attempt -lt 25; $attempt++) {
    Start-Sleep -Seconds 1
    & $python -B $tool --root $root --job-id $JobId --phase verify 2>$null
    if ($LASTEXITCODE -eq 0) { exit 0 }
}
& $python -B $tool --root $root --job-id $JobId --phase failed
throw 'Updated worker did not become ready; inspect installer.log'
