# Run only after the caller has verified the Oracle installer.
param([Parameter(Mandatory=$true)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProjectRoot)
if (-not $root.StartsWith('D:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Project must be on D:' }
Set-Location -LiteralPath $root
$env:TEMP = Join-Path $root '.cache\tmp'
$env:TMP = $env:TEMP
$env:VBOX_USER_HOME = Join-Path $root 'data\vm\virtualbox-config'
$dest = Join-Path $root 'data\vm\installers'
$result = Join-Path $dest 'host-install-result.json'
try {
    $installer = Join-Path $dest 'VirtualBox-7.2.20-175154-Win.exe'
    $sig = Get-AuthenticodeSignature -LiteralPath $installer
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Oracle America') { throw 'Invalid Oracle signature' }
    $expected = 'A81777D2B36380CE042A29E9C554CF032EB46A793F62E3CC82E7411E535C2C26'
    if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $expected) { throw 'Installer hash mismatch' }
    # -extract is the documented extraction mode; all extracted files stay in D: TEMP.
    $msiName = 'VirtualBox-7.2.20-r175154-MultiArch_amd64.msi'
    if (-not (Test-Path -LiteralPath (Join-Path $env:TEMP $msiName))) {
        $p = Start-Process -FilePath $installer -ArgumentList '-extract','-silent' -WindowStyle Hidden -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "Extraction failed: $($p.ExitCode)" }
    }
    $msis = @(Get-ChildItem -LiteralPath $env:TEMP -Filter $msiName -Recurse -File)
    if ($msis.Count -ne 1) { throw "Expected exactly one MSI, found $($msis.Count)" }
    $msi = $msis[0].FullName
    $msiSig = Get-AuthenticodeSignature -LiteralPath $msi
    if ($msiSig.Status -ne 'Valid' -or $msiSig.SignerCertificate.Subject -notmatch 'Oracle America') { throw 'Invalid MSI publisher' }
    # Verify the destination property against the actual package before installing.
    $wi = New-Object -ComObject WindowsInstaller.Installer
    $db = $wi.OpenDatabase($msi, 0)
    $view = $db.OpenView('SELECT `Directory` FROM `Directory`')
    $view.Execute()
    $found = $false
    while ($record = $view.Fetch()) { if ($record.StringData(1) -eq 'INSTALLDIR') { $found = $true } }
    $view.Close()
    if (-not $found) { throw 'Package does not expose expected INSTALLDIR; no installation attempted' }
    $log = Join-Path $dest 'virtualbox-install.log'
    $args = @('/i', ('"{0}"' -f $msi), '/qn', '/norestart', '/l*v', ('"{0}"' -f $log),
              'INSTALLDIR=D:\VirtualBox', 'ADDLOCAL=VBoxApplication',
              'VBOX_INSTALLDESKTOPSHORTCUT=0','VBOX_INSTALLQUICKLAUNCHSHORTCUT=0',
              'VBOX_REGISTERFILEEXTENSIONS=0','VBOX_START=0')
    $p = Start-Process -FilePath 'msiexec.exe' -ArgumentList $args -WindowStyle Hidden -Wait -PassThru
    if ($p.ExitCode -notin @(0,3010)) { throw "Installation failed: $($p.ExitCode); inspect D: installation log" }
    if (-not (Test-Path -LiteralPath 'D:\VirtualBox\VBoxManage.exe')) { throw 'VirtualBox not found at expected D: path' }
    @{ok=$true; reboot_required=($p.ExitCode -eq 3010); path='D:\VirtualBox'} | ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8
} catch {
    @{ok=$false; error=$_.Exception.Message} | ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8
    exit 1
}
