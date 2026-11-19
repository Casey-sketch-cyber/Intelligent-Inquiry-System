# This script runs INSIDE the paired VM only. Never changes a physical host disk.
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$pair = Get-Content -LiteralPath (Join-Path $source 'pairing.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$product = Get-CimInstance Win32_ComputerSystemProduct
if ($product.Name -ne 'VirtualBox' -or $product.UUID -ne $pair.vm_uuid) {
    throw 'This installer must run INSIDE the paired VirtualBox machine. No disks changed.'
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run InstallGuest.cmd as administrator INSIDE the virtual machine.'
}
if ($pair.allow_send -ne $false -or $pair.bridge -ne '\\VBOXSVR\TianxinBridge') { throw 'Expected read-only paired setup' }
if (-not (Test-Path -LiteralPath $pair.bridge)) { throw 'Install Guest Additions and reboot before running setup.' }

# Only the dedicated 36 GiB disk in this exact two-disk VM may be initialized.
$disks = @(Get-Disk)
$dataDisk = @($disks | Where-Object { $_.Number -eq 1 -and -not $_.IsBoot -and -not $_.IsSystem -and [math]::Abs($_.Size - 36GB) -lt 1MB })
$systemDisk = @($disks | Where-Object { $_.Number -eq 0 -and $_.IsBoot -and [math]::Abs($_.Size - 64GB) -lt 1MB })
if ($disks.Count -ne 2 -or $dataDisk.Count -ne 1 -or $systemDisk.Count -ne 1) {
    throw 'Unexpected VM disk layout. No disks changed; inspect manually.'
}
if ($dataDisk[0].PartitionStyle -eq 'RAW') {
    $dVolume = Get-CimInstance Win32_Volume -Filter "DriveLetter='D:'"
    if ($dVolume) {
        if ($dVolume.DriveType -ne 5 -or (Get-Volume -DriveLetter R -ErrorAction SilentlyContinue)) {
            throw 'D: is occupied by an unexpected volume; no disks changed.'
        }
        $dVolume | Set-CimInstance -Property @{DriveLetter='R:'} | Out-Null
    }
    $dataDisk[0] | Initialize-Disk -PartitionStyle GPT -PassThru |
        New-Partition -UseMaximumSize -DriveLetter D |
        Format-Volume -FileSystem NTFS -NewFileSystemLabel TianxinData -Confirm:$false | Out-Null
}
$partition = Get-Partition -DriveLetter D
$volume = Get-Volume -DriveLetter D
if ($partition.DiskNumber -ne 1 -or $volume.FileSystemLabel -ne 'TianxinData') {
    throw 'D: is not the dedicated Tianxin data disk. No application installed.'
}
$root = 'D:\TianxinRuntime'
if (Test-Path -LiteralPath $root) { throw 'Runtime already exists. Use the existing installation; setup will not overwrite it.' }
New-Item -ItemType Directory -Path "$root\.cache\tmp","$root\data\logs" -Force | Out-Null
$env:TEMP = "$root\.cache\tmp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PIP_CACHE_DIR = "$root\.cache\pip"
$env:PYTHONPYCACHEPREFIX = "$root\.cache\pycache"
$env:PYTHONIOENCODING = 'utf-8'
$log = "$root\data\logs\setup.log"
Start-Transcript -LiteralPath $log | Out-Null
try {
    $manifest = Get-Content -LiteralPath (Join-Path $source 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $manifest.PSObject.Properties) {
        $path = [IO.Path]::GetFullPath((Join-Path $source $entry.Name))
        if (-not $path.StartsWith($source.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid manifest path' }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.Value) { throw "Checksum failed: $($entry.Name)" }
    }
    Expand-Archive -LiteralPath (Join-Path $source 'application.zip') -DestinationPath $root
    $pythonInstaller = Join-Path $source 'python-3.12.10-amd64.exe'
    $sig = Get-AuthenticodeSignature -LiteralPath $pythonInstaller
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Invalid Python publisher' }
    $pythonHome = 'D:\TianxinPython'
    $p = Start-Process -FilePath $pythonInstaller -ArgumentList @('/quiet','InstallAllUsers=0',
        "TargetDir=$pythonHome",'Include_launcher=0','Include_test=0','Include_doc=0','Include_tcltk=0',
        'AssociateFiles=0','Shortcuts=0','PrependPath=0','CompileAll=0','/log',"$root\data\logs\python-install.log") -WindowStyle Hidden -Wait -PassThru
    if ($p.ExitCode -notin @(0,3010)) { throw "Python installation failed: $($p.ExitCode)" }
    & "$pythonHome\python.exe" -B -m venv "$root\.venv"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create runtime' }
    & "$root\.venv\Scripts\python.exe" -B -m pip install --no-index --find-links (Join-Path $source 'wheels') -r "$root\requirements.lock.txt"
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    $seed = Join-Path $source 'seed'
    if (Test-Path -LiteralPath $seed) {
        Get-ChildItem -LiteralPath $seed -Force | Where-Object {$_.Name -ne 'inquiry.db'} |
            Copy-Item -Destination $root -Recurse -Force
        if (Test-Path -LiteralPath "$seed\inquiry.db") { Copy-Item -LiteralPath "$seed\inquiry.db" -Destination "$root\data\inquiry.db" }
    } else {
        Copy-Item -LiteralPath "$root\settings.example.toml" -Destination "$root\settings.toml"
        Copy-Item -LiteralPath "$root\.env.example" -Destination "$root\.env"
    }
    Copy-Item -LiteralPath (Join-Path $source 'pairing.json') -Destination "$root\data\vm_guest.json"
    # Auto-start only after this guest user logs in. No auto-login passwords stored.
    $run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-ItemProperty -Path $run -Name 'TianxinGuest' -PropertyType String -Value ('wscript.exe "{0}\vmhost\start_guest.vbs"' -f $root) -Force | Out-Null
    Write-Output 'Setup complete. Install QQ on D:, log in to Windows and QQ, then start the Tianxin guest service.'
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"{0}\vmhost\start_guest.vbs"' -f $root) -WindowStyle Hidden
} finally { Stop-Transcript | Out-Null }
