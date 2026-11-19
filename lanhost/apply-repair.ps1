$ErrorActionPreference = 'Stop'
$tool = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $tool.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Put this repair tool on D:' }
$env:TEMP = Join-Path $tool '.cache\tmp'
$env:TMP = $env:TEMP
$env:PSModuleAnalysisCachePath = Join-Path $tool '.cache\powershell\ModuleAnalysisCache'
New-Item -ItemType Directory -Path $env:TEMP,(Split-Path -Parent $env:PSModuleAnalysisCachePath) -Force | Out-Null
. (Join-Path $tool 'install_helpers.ps1')
$target = Split-Path -Parent $tool
if (-not (Test-Path -LiteralPath (Join-Path $target 'application.zip')) -or
    -not (Test-Path -LiteralPath (Join-Path $target 'manifest.json'))) {
    throw 'Move the repair-tools folder INTO the original extracted installation package (beside application.zip and manifest.json), then run it again.'
}
$patch = Get-Content -LiteralPath (Join-Path $tool 'repair.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$managed = @('install.ps1','install_helpers.ps1')
if ($null -ne $patch.files.PSObject.Properties['python.3.12.10.nupkg']) { $managed += 'python.3.12.10.nupkg' }
if ((Get-SetupDigest (Join-Path $target 'application.zip')) -ne $patch.application_sha256) {
    throw 'This repair tool does not match this application package. Nothing was replaced.'
}
foreach ($name in $managed) {
    if ((Get-SetupDigest (Join-Path $tool $name)) -ne $patch.files.PSObject.Properties[$name].Value) { throw 'Repair file checksum failed' }
}
$manifestPath = Join-Path $target 'manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in $manifest.PSObject.Properties) {
    $path = Assert-DSetupPath (Join-Path $target $entry.Name)
    if (-not $path.StartsWith($target.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe manifest path' }
    $actual = Get-SetupDigest $path
    $replacement = $patch.files.PSObject.Properties[$entry.Name]
    if ($actual -ne $entry.Value -and ($null -eq $replacement -or $actual -ne $replacement.Value)) {
        throw "Original package checksum failed: $($entry.Name). Nothing was replaced."
    }
}
$backup = Assert-DSetupPath (Join-Path $target ('.installer-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')))
New-Item -ItemType Directory -Path $backup | Out-Null
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $backup 'manifest.json')
foreach ($name in $managed) {
    $destination = Assert-DSetupPath (Join-Path $target $name)
    if (Test-Path -LiteralPath $destination) { Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $name) }
    Copy-Item -LiteralPath (Join-Path $tool $name) -Destination $destination -Force
    $manifest | Add-Member -MemberType NoteProperty -Name $name -Value $patch.files.PSObject.Properties[$name].Value -Force
}
$temporary = $manifestPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
try {
    [IO.File]::WriteAllText($temporary, ($manifest | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $manifestPath -Force
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
Write-Output "Installer repaired. Original installer backup: $backup"
Write-Output 'Continuing installation. No business database, account settings or pairing files were removed.'
& (Join-Path $target 'install.ps1')
