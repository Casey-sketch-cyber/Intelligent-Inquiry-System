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
$tool = Join-Path $PSScriptRoot 'support_bootstrap.py'
Write-Output 'Checking this paired worker and installed source...'
$stateJson = & $python -B $tool --root $root --package $PSScriptRoot --phase prepare
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap verification failed; nothing was changed' }
$state = $stateJson | ConvertFrom-Json
$owners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort ([int]$state.ui_port) -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
if ($owners.Count -ne 1) { throw 'Worker listener identity is ambiguous' }
$workerProcessId = [int]$owners[0]
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$workerProcessId"
if (-not $process.ExecutablePath -or @($state.process_executables) -inotcontains $process.ExecutablePath -or $process.CommandLine -notmatch '(?i)(?:^|\s)-m\s+lanhost\.worker(?:\s|$)') { throw 'Unexpected worker process; nothing was stopped' }
$stopped = $false
try {
    Stop-Process -Id $workerProcessId -ErrorAction Stop
    $stopped = $true
    Wait-Process -Id $workerProcessId -ErrorAction SilentlyContinue
    Write-Output 'Backing up the database and source; installing diagnostics...'
    & $python -B $tool --root $root --package $PSScriptRoot --phase install
    if ($LASTEXITCODE -ne 0) { throw 'Bootstrap installation failed; keep this window and backups' }
} finally {
    if ($stopped) { & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$root\lanhost\start_worker.ps1" -NoBrowser }
}
for ($attempt=0; $attempt -lt 25; $attempt++) {
    Start-Sleep -Seconds 1
    $verifiedJson = & $python -B $tool --root $root --package $PSScriptRoot --phase verify 2>$null
    if ($LASTEXITCODE -eq 0) {
        $verified = $verifiedJson | ConvertFrom-Json
        if ($verified.rpc_port -ne $state.rpc_port) { Write-Warning 'RPC port changed. Re-export the connection file and allow private LAN access.' }
        Write-Output 'SUCCESS: paired remote diagnostics and application updates are ready.'
        Write-Output 'Leave this QQ worker running. Testing is controlled from the office PC.'
        exit 0
    }
}
throw 'New service could not be verified; keep this window and inspect worker logs'
