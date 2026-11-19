param([string]$WorkerRoot = 'D:\TianxinWorker', [string]$RequestPath = (Join-Path $PSScriptRoot 'reset-request.json'))
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($WorkerRoot).TrimEnd('\')
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Worker must be on D:' }
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PSModuleAnalysisCachePath = "$root\.cache\powershell\ModuleAnalysisCache"
$env:PYTHONIOENCODING = 'utf-8'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator +
    (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
Set-Location -LiteralPath $root
$python = "$root\.venv\Scripts\python.exe"
$tool = Join-Path $PSScriptRoot 'reset_history.py'
$windowsPS = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
# Keep installed code, accounts, pairing, QQ, browser data and send settings.
$paused = & $python -B $tool --root $root --request $RequestPath --phase pause
if ($LASTEXITCODE -ne 0) { throw 'Could not pause the paired worker; nothing was deleted' }
$state = $paused | ConvertFrom-Json
if ($state.manual_ready -ne $true -or $state.allow_send -ne $false) { throw 'Worker must be paused and read-only' }
$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort ([int]$state.ui_port) -State Listen)
$owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
if ($owners.Count -ne 1) { throw 'Worker panel process could not be identified; nothing was deleted' }
$workerProcessId = [int]$owners[0]
$workerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$workerProcessId"
$command = [string]$workerProcess.CommandLine
$allowedExecutables = @($state.process_executables | ForEach-Object { [IO.Path]::GetFullPath($_) })
if (-not $workerProcess.ExecutablePath -or
    $allowedExecutables -inotcontains [IO.Path]::GetFullPath($workerProcess.ExecutablePath) -or
    $command -notmatch '(?i)(?:^|\s)-m\s+lanhost\.worker(?:\s|$)') {
    throw 'Panel is not the expected D: worker process; nothing was stopped or deleted'
}
$stopped = $false
try {
    Write-Output 'Paired worker is paused. Stopping its service before backing up history...'
    Stop-Process -Id $workerProcessId -ErrorAction Stop
    $stopped = $true
    Wait-Process -Id $workerProcessId -ErrorAction SilentlyContinue
    $cleared = & $python -B $tool --root $root --request $RequestPath --phase clear
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup stopped; inspect the error above. Backup files were kept.' }
    $result = $cleared | ConvertFrom-Json
    Write-Output ("Old inquiry tasks cleared: " + $result.before.task)
    Write-Output ("Backup: " + $result.backup)
    if (@($result.reports_not_removed).Count -gt 0) {
        Write-Warning 'Some report files remain open; their backed-up originals could not be removed from the report list.'
    }
} finally {
    if ($stopped) {
        & $windowsPS -NoProfile -ExecutionPolicy Bypass -File "$root\lanhost\start_worker.ps1" -NoBrowser
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Restart failed. Run D:\TianxinWorker\Start.cmd manually.' }
    }
}
$verified = $null
for ($attempt = 0; $attempt -lt 25; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $json = & $python -B $tool --root $root --request $RequestPath --phase verify 2>$null
        if ($LASTEXITCODE -eq 0) { $verified = $json | ConvertFrom-Json; break }
    } catch { continue }
}
if (-not $verified) { throw 'Cleanup finished, but worker restart was not verified. Run Start.cmd and keep it paused.' }
if ($verified.tasks -ne 0) { throw 'Unexpected tasks found after restart; no new inquiry was started by this tool' }
Write-Output 'Verified: tasks=0, worker paused, real sending locked. Refresh the office workbench.'
if ($verified.rpc_port -ne $state.rpc_port) {
    Write-Warning 'RPC port changed. Run 2_允许办公电脑连接.cmd, then import the updated connection JSON on the office PC.'
}
