# Shared by the installer and its isolated recovery tests. No GUI or QQ operations.
# Use modules belonging to this PowerShell version, not inherited PowerShell 7 paths.
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator +
    (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')

function Assert-DSetupPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw "Setup data must stay on D: $full" }
    $cursor = $full
    while ($cursor.Length -gt 3) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Setup will not follow a junction or link: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}

function Get-SetupDigest([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}

function Invoke-SetupModule([string]$Python, [string]$Root, [string]$Module, [string[]]$Arguments=@()) {
    Push-Location -LiteralPath (Assert-DSetupPath $Root)
    try {
        & $Python -B -m $Module @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Python module failed: $Module (exit $LASTEXITCODE). See the setup log." }
    } finally { Pop-Location }
}

function Get-SetupPythonPackageHash {
    return '0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8'
}

function Install-SetupPythonPackage([string]$Package, [string]$Root, [string]$ExpectedHash) {
    if ((Get-SetupDigest $Package) -ne $ExpectedHash) { throw 'Portable Python checksum failed; no runtime was changed' }
    $destination = Assert-DSetupPath (Join-Path $Root 'python-portable\3.12.10')
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Package)
    try {
        foreach ($entry in $zip.Entries) {
            if (-not $entry.FullName.StartsWith('tools/') -or $entry.FullName.EndsWith('/')) { continue }
            $name = $entry.FullName.Substring(6).Replace('\','/')
            if ($name.Split('/') -contains '..' -or $name.Contains(':') -or $name.StartsWith('/')) { throw 'Unsafe portable runtime entry' }
            $path = Assert-DSetupPath (Join-Path $destination $name)
            if (-not $path.StartsWith($destination + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Portable runtime escaped destination' }
            if (Test-Path -LiteralPath $path) {
                $stream = $entry.Open(); $hash = [Security.Cryptography.SHA256]::Create()
                try { $digest = ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
                finally { $stream.Dispose(); $hash.Dispose() }
                if ((Get-SetupDigest $path) -ne $digest) { throw "Existing portable runtime file differs: $name. File preserved." }
                continue
            }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
            $temporary = $path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
            try {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $temporary)
                [IO.File]::Move($temporary, $path)
            } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
        }
        foreach ($name in @('python.exe','python312.dll','Lib\venv\__init__.py','Lib\ensurepip\__init__.py')) {
            if (-not (Test-Path -LiteralPath (Join-Path $destination $name))) { throw "Incomplete portable runtime: $name" }
        }
    } finally { $zip.Dispose() }
    return $destination
}

function Initialize-SetupVenv([string]$Base, [string]$Root, [bool]$Enrolled) {
    $venv = Assert-DSetupPath (Join-Path $Root '.venv')
    if (Test-Path -LiteralPath $venv) {
        $venvHome = ''
        if (Test-Path -LiteralPath (Join-Path $venv 'pyvenv.cfg')) {
            foreach ($line in Get-Content -LiteralPath (Join-Path $venv 'pyvenv.cfg') -Encoding UTF8) {
                if ($line -match '^home\s*=\s*(.+)$') { $venvHome = $Matches[1].Trim() }
            }
        }
        $matching = $venvHome -and [IO.Path]::IsPathRooted($venvHome) -and [IO.Path]::GetFullPath($venvHome) -eq [IO.Path]::GetFullPath($Base)
        if (-not $matching -or -not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
            if ($Enrolled) { throw 'An existing paired virtual environment differs. It was preserved; inspect the setup log.' }
            $backup = Assert-DSetupPath (Join-Path $Root ('data\setup-backups\venv-' + [Guid]::NewGuid().ToString('N')))
            $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
            if (-not $venv.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
                -not $backup.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Virtual environment backup escaped runtime' }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($backup))
            Move-Item -LiteralPath $venv -Destination $backup
            Write-Host "Incomplete virtual environment preserved at $backup"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
        & (Join-Path $Base 'python.exe') -I -B -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Portable virtual environment creation failed. See the setup log.' }
    }
}

function Get-SetupApplication([string]$Archive) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\','/')
            if ($name.EndsWith('/')) { continue }
            if ($name -notmatch '^(main\.py|requirements\.lock\.txt|settings\.example\.toml|\.env\.example|(inquiry|ui|vmhost|lanhost)/.+)$' -or
                $name.Split('/') -contains '..' -or $name.Contains(':')) { throw "Unsafe application entry: $name" }
            $stream = $entry.Open()
            $hash = [Security.Cryptography.SHA256]::Create()
            try { $digest = ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
            finally { $stream.Dispose(); $hash.Dispose() }
            [pscustomobject]@{Name=$name; Hash=$digest}
        }
    } finally { $zip.Dispose() }
}

function Get-SetupPlan([string]$Root, [string]$Role, [string]$Identity, [string]$ControllerId, [string]$Archive, $Entries) {
    $Root = Assert-DSetupPath $Root
    $appHash = Get-SetupDigest $Archive
    $statePath = Join-Path $Root 'data\setup-state.json'
    $resume = Test-Path -LiteralPath $Root
    $enrolled = $false
    if ($resume) {
        if (-not (Get-Item -LiteralPath $Root -Force).PSIsContainer) { throw 'The runtime path is not a folder' }
        foreach ($name in @('data','data\logs','data\setup-state.json','data\worker.json','data\lan\controller.json',
                            '.cache\tmp','.cache\powershell','python','python-portable','.venv','offline')) {
            [void](Assert-DSetupPath (Join-Path $Root $name))
        }
        foreach ($entry in $Entries) {
            $path = Assert-DSetupPath (Join-Path $Root $entry.Name)
            if ((Test-Path -LiteralPath $path) -and (Get-SetupDigest $path) -ne $entry.Hash) {
                throw "Existing application differs from this package: $($entry.Name). Files were preserved. Use the original package or inspect the setup log."
            }
        }
        $worker = Join-Path $Root 'data\worker.json'
        $office = Join-Path $Root 'data\lan\controller.json'
        if (Test-Path -LiteralPath (Join-Path $Root 'data\vm_guest.json')) { throw 'This is a virtual-machine runtime; files were preserved' }
        if ($Role -eq 'worker') {
            if (Test-Path -LiteralPath $office) { throw 'This folder belongs to an office controller; files were preserved' }
            if (Test-Path -LiteralPath $worker) {
                $cfg = Get-Content -LiteralPath $worker -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($cfg.role -ne 'worker' -or $cfg.worker_device -ne $Identity -or $cfg.controller_id -ne $ControllerId -or
                    $cfg.allow_send -isnot [bool] -or $cfg.allow_send -ne $false -or
                    [IO.Path]::GetFullPath([string]$cfg.runtime) -ne $Root) { throw 'Existing worker pairing does not match; files were preserved' }
                $enrolled = $true
            }
        } else {
            if (Test-Path -LiteralPath $worker) { throw 'This folder belongs to a QQ worker; files were preserved' }
            if (Test-Path -LiteralPath $office) {
                $cfg = Get-Content -LiteralPath $office -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($cfg.office_device -ne $Identity) { throw 'Existing office pairing belongs to another device' }
                $enrolled = $true
            }
        }
        if (Test-Path -LiteralPath $statePath) {
            $old = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($old.version -ne 1 -or $old.role -ne $Role -or $old.device -ne $Identity -or
                $old.controller_id -ne $ControllerId -or $old.application_hash -ne $appHash) {
                throw 'Existing installation belongs to another package or device; files were preserved'
            }
        } elseif (@(Get-ChildItem -LiteralPath $Root -Force).Count -gt 0) {
            $knownApp = (Test-Path -LiteralPath (Join-Path $Root 'main.py')) -and
                (Test-Path -LiteralPath (Join-Path $Root 'requirements.lock.txt')) -and
                ((Test-Path -LiteralPath (Join-Path $Root 'data\logs\setup.log')) -or $enrolled)
            if (-not $knownApp) {
                # The old installer created cache/log folders before extracting any code.
                foreach ($item in Get-ChildItem -LiteralPath $Root -Recurse -Force) {
                    [void](Assert-DSetupPath $item.FullName)
                    $relative = $item.FullName.Substring($Root.Length + 1).Replace('\','/')
                    if ($relative -notmatch '^(\.cache(/.*)?|data|data/logs(/.*)?)$') {
                        throw 'Unrecognized existing folder. Nothing was replaced; keep it and inspect data\logs\setup.log.'
                    }
                }
            }
        }
    }
    return [pscustomobject]@{Resume=$resume; Enrolled=$enrolled; State=@{
        version=1; role=$Role; device=$Identity; controller_id=$ControllerId; application_hash=$appHash;
        status='installing'; phase='checked'; updated_at=[DateTime]::UtcNow.ToString('o')
    }}
}

function Save-SetupState([string]$Root, $State, [string]$Phase, [string]$Status='installing') {
    $path = Assert-DSetupPath (Join-Path $Root 'data\setup-state.json')
    $State.phase=$Phase; $State.status=$Status; $State.updated_at=[DateTime]::UtcNow.ToString('o')
    $temporary = $path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $path -Force
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
}

function Copy-SetupApplication([string]$Archive, [string]$Root, $Entries) {
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $Entries) {
            $destination = Assert-DSetupPath (Join-Path $Root $entry.Name)
            if (Test-Path -LiteralPath $destination) { continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
            $temporary = $destination + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
            try {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($zip.GetEntry($entry.Name), $temporary)
                if ((Get-SetupDigest $temporary) -ne $entry.Hash) { throw 'Application extraction checksum failed' }
                [IO.File]::Move($temporary, $destination)
            } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
        }
    } finally { $zip.Dispose() }
}

function Copy-SetupInitialFile([string]$Source, [string]$Destination, [string]$Example='', [string]$BackupFolder='') {
    $Destination = Assert-DSetupPath $Destination
    $replaceExample = $false
    if (Test-Path -LiteralPath $Destination) {
        if (-not $Example -or -not (Test-Path -LiteralPath $Example) -or
            (Get-SetupDigest $Destination) -ne (Get-SetupDigest $Example) -or
            (Get-SetupDigest $Destination) -eq (Get-SetupDigest $Source)) { return }
        [void](Assert-DSetupPath $BackupFolder)
        [void][IO.Directory]::CreateDirectory($BackupFolder)
        $backup = Join-Path $BackupFolder ([IO.Path]::GetFileName($Destination) + '.' + [Guid]::NewGuid().ToString('N') + '.bak')
        $replaceExample = $true
    }
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Destination))
    $temporary = $Destination + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        Copy-Item -LiteralPath $Source -Destination $temporary
        if ((Get-SetupDigest $temporary) -ne (Get-SetupDigest $Source)) { throw 'Initial data copy checksum failed' }
        if ($replaceExample) { [IO.File]::Replace($temporary, $Destination, $backup) }
        else { [IO.File]::Move($temporary, $Destination) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
}
