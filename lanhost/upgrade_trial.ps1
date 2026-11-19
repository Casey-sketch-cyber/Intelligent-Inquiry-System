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
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
Set-Location -LiteralPath $root
$python = "$root\.venv\Scripts\python.exe"
$tool = Join-Path $PSScriptRoot 'upgrade_trial.py'
$stateJson = & $python -B $tool --root $root --package $PSScriptRoot --phase prepare
if ($LASTEXITCODE -ne 0) { throw 'Worker or source verification failed; nothing was changed' }
$state = $stateJson | ConvertFrom-Json
$owners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort ([int]$state.ui_port) -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
if ($owners.Count -ne 1) { throw 'Worker listener identity is ambiguous' }
$workerProcessId = [int]$owners[0]
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$workerProcessId"
if (-not $process.ExecutablePath -or @($state.process_executables) -inotcontains $process.ExecutablePath -or
    $process.CommandLine -notmatch '(?i)(?:^|\s)-m\s+lanhost\.worker(?:\s|$)') { throw 'Unexpected worker process; nothing was stopped' }
$stopped = $false
try {
    Stop-Process -Id $workerProcessId -ErrorAction Stop
    $stopped = $true
    Wait-Process -Id $workerProcessId -ErrorAction SilentlyContinue
    & $python -B $tool --root $root --package $PSScriptRoot --phase install
    if ($LASTEXITCODE -ne 0) { throw 'Upgrade failed. Keep backups and this window.' }
} finally {
    if ($stopped) {
        & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$root\lanhost\start_worker.ps1" -NoBrowser
    }
}
$ready = $false
for ($attempt = 0; $attempt -lt 25; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $verifiedJson = & $python -B $tool --root $root --package $PSScriptRoot --phase verify 2>$null
        if ($LASTEXITCODE -eq 0) { $verified = $verifiedJson | ConvertFrom-Json; $ready=$true; break }
    } catch { continue }
}
if (-not $ready) { throw 'Upgraded service was not verified; no live trial was started' }
if ($verified.rpc_port -ne $state.rpc_port) { Write-Warning 'RPC port changed: allow private LAN access again, then import the updated connection file on the office PC.' }
& $python -B $tool --root $root --package $PSScriptRoot --phase launch
if ($LASTEXITCODE -ne 0) { throw 'Trial acceptance was not verified. Inspect tasks; do not retry automatically.' }
Write-Output 'Upgrade completed. The authorized 3-suppliers-per-model trial is running. Use Pause to stop.'
