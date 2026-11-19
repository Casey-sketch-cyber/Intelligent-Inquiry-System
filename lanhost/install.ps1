$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $source.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Extract the deployment package on D: first.' }
$env:TEMP = Join-Path $source '.setup-cache\tmp'
$env:TMP = $env:TEMP
$env:PSModuleAnalysisCachePath = Join-Path $source '.setup-cache\powershell\ModuleAnalysisCache'
New-Item -ItemType Directory -Path $env:TEMP,(Split-Path -Parent $env:PSModuleAnalysisCachePath) -Force | Out-Null
. (Join-Path $source 'install_helpers.ps1')
$role = (Get-Content -LiteralPath (Join-Path $source 'role.json') -Raw -Encoding UTF8 | ConvertFrom-Json).role
if ($role -notin @('office','worker')) { throw 'Invalid deployment role' }
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) { throw '64-bit Windows and Windows PowerShell are required' }
$bios = ((Get-CimInstance Win32_ComputerSystemProduct).UUID).Trim().ToLowerInvariant()
$guid = ((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid).Trim().ToLowerInvariant()
$hasher = [Security.Cryptography.SHA256]::Create()
try { $identity = ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($bios + '|' + $guid)))).Replace('-','').ToLowerInvariant() }
finally { $hasher.Dispose() }
$controllerId = ''
if ($role -eq 'worker') {
    $pair = Get-Content -LiteralPath (Join-Path $source 'bootstrap.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($pair.office_device -notmatch '^[a-f0-9]{64}$' -or $pair.office_device -eq $identity) {
        throw 'The QQ worker must be ANOTHER computer. This office computer was not changed.'
    }
    $controllerId = [string]$pair.controller_id
    [void][Guid]::Parse($controllerId)
}
# Verify materials before creating a runtime, including the recovery helper.
$manifest = Get-Content -LiteralPath (Join-Path $source 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($required in @('application.zip','role.json','install.ps1','install_helpers.ps1','python.3.12.10.nupkg')) {
    if ($null -eq $manifest.PSObject.Properties[$required]) { throw "Missing checksum entry: $required" }
}
foreach ($entry in $manifest.PSObject.Properties) {
    $path = Assert-DSetupPath (Join-Path $source $entry.Name)
    if (-not $path.StartsWith($source.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid manifest path' }
    if ((Get-SetupDigest $path) -ne $entry.Value) { throw "Checksum failed: $($entry.Name)" }
}
if ($role -eq 'worker') {
    $qq = Join-Path $source 'QQ_9.9.36_260924_x64_01.exe'
    $qqSignature = Get-AuthenticodeSignature -LiteralPath $qq
    if ($qqSignature.Status -ne 'Valid' -or $qqSignature.SignerCertificate.Subject -notmatch 'Tencent') { throw 'Invalid QQ publisher' }
}
$root = if ($role -eq 'worker') { 'D:\TianxinWorker' } else { 'D:\TianxinOffice' }
[void](Assert-DSetupPath ($root + '.setup.lock'))
$setupLock = [IO.FileStream]::new(($root + '.setup.lock'), [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
$transcript = $false
$plan = $null
try {
    $entries = @(Get-SetupApplication (Join-Path $source 'application.zip'))
    $plan = Get-SetupPlan $root $role $identity $controllerId (Join-Path $source 'application.zip') $entries
    if ($plan.Enrolled -and (Test-Path -LiteralPath "$root\Start.cmd") -and
        (Test-Path -LiteralPath "$root\.venv\Scripts\python.exe")) {
        $verify = if ($role -eq 'worker') { 'verify-worker' } else { 'office' }
        Invoke-SetupModule "$root\.venv\Scripts\python.exe" $root 'lanhost.setup' @($verify)
        Write-Output 'This runtime is already installed and paired. Opening it without reinstalling or replacing business data.'
        $existingEntry = if ($role -eq 'worker') { 'start_worker.ps1' } else { 'start_controller.ps1' }
        & "$root\lanhost\$existingEntry"
        return
    }
    New-Item -ItemType Directory -Path "$root\.cache\tmp","$root\.cache\powershell","$root\data\logs" -Force | Out-Null
    foreach ($name in @('TEMP','TMP','TMPDIR')) { Set-Item -LiteralPath "Env:$name" -Value "$root\.cache\tmp" }
    $env:PIP_CACHE_DIR = "$root\.cache\pip"
    $env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
    $env:PSModuleAnalysisCachePath = "$root\.cache\powershell\ModuleAnalysisCache"
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item -LiteralPath Env:PYTHONHOME,Env:PYTHONPATH -ErrorAction SilentlyContinue
    $log = "$root\data\logs\setup-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff').log"
    Start-Transcript -LiteralPath $log | Out-Null
    $transcript = $true
    if ($plan.Resume) { Write-Output 'Continuing the existing installation. Existing business data and pairing are preserved.' }
    Save-SetupState $root $plan.State 'application'
    Copy-SetupApplication (Join-Path $source 'application.zip') $root $entries
    Save-SetupState $root $plan.State 'python'
    Write-Output 'Preparing private portable Python on D:. The system-wide Python installer is not used.'
    $base = Install-SetupPythonPackage (Join-Path $source 'python.3.12.10.nupkg') $root (Get-SetupPythonPackageHash)
    foreach ($binary in @('python.exe','python312.dll')) {
        $runtimeSignature = Get-AuthenticodeSignature -LiteralPath (Join-Path $base $binary)
        if ($runtimeSignature.Status -ne 'Valid' -or $runtimeSignature.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Portable Python publisher is invalid; no interpreter was started' }
    }
    & "$base\python.exe" -I -S -B -c 'import sys,pathlib; assert sys.version_info[:2] == (3,12); assert pathlib.Path(sys.base_prefix).resolve() == pathlib.Path(sys.argv[1]).resolve()' $base
    if ($LASTEXITCODE -ne 0) { throw 'Existing Python runtime is incompatible; files were preserved' }
    Initialize-SetupVenv $base $root $plan.Enrolled
    & "$root\.venv\Scripts\python.exe" -I -B -c 'import sys,pathlib; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve(); assert pathlib.Path(sys.base_prefix).resolve() == pathlib.Path(sys.argv[2]).resolve()' "$root\.venv" $base
    if ($LASTEXITCODE -ne 0) { throw 'Existing virtual environment points elsewhere; files were preserved' }
    Save-SetupState $root $plan.State 'dependencies'
    Invoke-SetupModule "$root\.venv\Scripts\python.exe" $root 'pip' @('install','--no-index','--no-compile','--disable-pip-version-check','--find-links',(Join-Path $source 'wheels'),'-r',"$root\requirements.lock.txt")
    Save-SetupState $root $plan.State 'initial-data'
    $seed = Join-Path $source 'seed'
    foreach ($pairing in @(@('.env','.env.example'),@('settings.toml','settings.example.toml'))) {
        $private = Join-Path $seed $pairing[0]
        $example = Join-Path $root $pairing[1]
        if ($role -eq 'worker' -and (Test-Path -LiteralPath $private) -and -not $plan.Enrolled) {
            Copy-SetupInitialFile $private (Join-Path $root $pairing[0]) $example "$root\data\setup-backups"
        } else { Copy-SetupInitialFile $example (Join-Path $root $pairing[0]) }
    }
    if ($role -eq 'worker' -and (Test-Path -LiteralPath $seed) -and -not $plan.Enrolled) {
        foreach ($file in Get-ChildItem -LiteralPath $seed -File -Recurse -Force) {
            $relative = $file.FullName.Substring($seed.Length + 1)
            if ($relative -in @('.env','settings.toml')) { continue }
            $destination = if ($relative -eq 'inquiry.db') { "$root\data\inquiry.db" } else { Join-Path $root $relative }
            Copy-SetupInitialFile $file.FullName $destination
        }
    }
    Save-SetupState $root $plan.State 'enrollment'
    if ($role -eq 'worker') {
        if ($plan.Enrolled) { Invoke-SetupModule "$root\.venv\Scripts\python.exe" $root 'lanhost.setup' @('verify-worker') }
        else { Invoke-SetupModule "$root\.venv\Scripts\python.exe" $root 'lanhost.setup' @('worker','--bootstrap',(Join-Path $source 'bootstrap.json')) }
        Copy-SetupInitialFile $qq "$root\data\QQ-install.exe"
        $run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
        if (-not (Test-Path -LiteralPath $run)) { New-Item -Path $run -Force | Out-Null }
        $start = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}\lanhost\start_worker.ps1" -NoBrowser' -f $root
        New-ItemProperty -Path $run -Name 'TianxinWorker' -PropertyType String -Value $start -Force | Out-Null
    } else {
        $offline = "$root\offline"
        foreach ($name in @('python.3.12.10.nupkg','QQ_9.9.36_260924_x64_01.exe')) { Copy-SetupInitialFile (Join-Path $source $name) (Join-Path $offline $name) }
        foreach ($wheel in Get-ChildItem -LiteralPath (Join-Path $source 'wheels') -Filter *.whl) { Copy-SetupInitialFile $wheel.FullName (Join-Path $offline ('wheels\' + $wheel.Name)) }
        Invoke-SetupModule "$root\.venv\Scripts\python.exe" $root 'lanhost.setup' @('office')
    }
    $entry = if ($role -eq 'worker') { 'start_worker.ps1' } else { 'start_controller.ps1' }
    $cmd = "@echo off`r`ncd /d `"$root`"`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$root\lanhost\$entry`"`r`nif errorlevel 1 pause`r`n"
    if (-not (Test-Path -LiteralPath "$root\Start.cmd")) { [IO.File]::WriteAllText("$root\Start.cmd",$cmd,[Text.Encoding]::ASCII) }
    Save-SetupState $root $plan.State 'complete' 'complete'
    Write-Output "Setup complete: $root. Use Start.cmd on this computer. Real sending remains locked."
    & "$root\lanhost\$entry"
} catch {
    if ($plan -and $transcript) { Save-SetupState $root $plan.State $plan.State.phase 'failed'; Write-Output "Setup stopped. Keep all existing files. Log: $log" }
    throw
} finally {
    if ($transcript) { Stop-Transcript | Out-Null }
    $setupLock.Dispose()
}
