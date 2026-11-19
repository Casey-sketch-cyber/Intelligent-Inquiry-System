$ErrorActionPreference = 'Stop'
$root = 'D:\TianxinWorker'
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PSModuleAnalysisCachePath = "$root\.cache\powershell\ModuleAnalysisCache"
Set-Location -LiteralPath $root
& "$root\.venv\Scripts\python.exe" -B -m lanhost.setup verify-worker
if ($LASTEXITCODE -ne 0) { throw 'Worker device verification failed' }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $PSCommandPath + '"'))
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'The connection rule was not confirmed or could not be created.' }
    Write-Output 'Private LAN connection permission configured.'
    exit 0
}
$state = Get-Content -LiteralPath "$root\data\lan\worker-ui.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$port = [int]$state.port
if ($port -lt 8778 -or $port -ge 8798) { throw 'Unexpected worker port' }
$name = "TianxinQQWorker-$port"
if (-not (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $name -DisplayName 'Tianxin QQ worker - private LAN only' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -Program "$root\.venv\Scripts\python.exe" -Profile Private -RemoteAddress LocalSubnet | Out-Null
}
Write-Output "Allowed TCP $port for this QQ worker on private local subnets. Windows Firewall remains enabled."
