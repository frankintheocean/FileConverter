param(
    [string]$Installer = "$PSScriptRoot\..\artifacts\FileConverter-1.0.5-Windows-x64-Setup.exe",
    [string]$PreviousInstaller = ''
)
$ErrorActionPreference = 'Stop'
$Installer = (Resolve-Path $Installer).Path
$UpgradeVerified = $false
if ($PreviousInstaller -ne '') { $PreviousInstaller = (Resolve-Path $PreviousInstaller).Path }
$InstallPath = Join-Path $env:LOCALAPPDATA 'Programs\FileConverter'
$DataPath = Join-Path $env:LOCALAPPDATA 'FileConverter'
if (Test-Path $InstallPath) { throw 'Use a clean disposable VM; an installation already exists.' }
function Run-Setup {
    $process = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=desktopicon' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Setup failed: $($process.ExitCode)" }
}
function Assert-File([string]$Path) { if (!(Test-Path $Path)) { throw "Missing: $Path" } }
if ($PreviousInstaller -ne '') {
    $previous = Start-Process -FilePath $PreviousInstaller -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=desktopicon' -Wait -PassThru
    if ($previous.ExitCode -ne 0) { throw 'Prior version installation failed' }
    New-Item -ItemType Directory -Force $DataPath | Out-Null
    Set-Content (Join-Path $DataPath 'upgrade-preservation.txt') 'Keep upgrade user data'
    python "$PSScriptRoot/windows_data_check.py" --seed
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare user settings/presets/queue preservation check' }
}
Run-Setup
if ($PreviousInstaller -ne '') {
    Assert-File (Join-Path $DataPath 'upgrade-preservation.txt')
    $version = Get-ItemProperty 'HKCU:\Software\FileConverter'
    if ($version.Version -ne '1.0.5') { throw 'Upgrade version metadata incorrect' }
    $UpgradeVerified = $true
}
$Exe = Join-Path $InstallPath 'FileConverter.exe'
$Desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'FileConverter.lnk'
$Menu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\FileConverter\FileConverter.lnk'
Assert-File $Exe
Assert-File $Desktop
Assert-File $Menu
$registration = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{A6B86057-4065-44EC-B12E-EC867BE5C486}_is1'
if ($registration.DisplayName -ne 'FileConverter') { throw 'Registration incorrect' }
$metadata = (Get-Item $Exe).VersionInfo
if ($metadata.ProductName -ne 'FileConverter') { throw 'Executable identity incorrect' }
$Hash = (Get-FileHash $Exe).Hash
$EngineHash = (Get-FileHash (Join-Path $InstallPath 'vendor\ffmpeg.exe')).Hash
New-Item -ItemType Directory -Force $DataPath | Out-Null
Set-Content (Join-Path $DataPath 'preservation.txt') 'Preserve user settings'
$UserOutput = Join-Path $env:TEMP 'FileConverter-user-output.txt'
Set-Content $UserOutput 'Never uninstall user output'
Remove-Item $Menu
Set-Content (Join-Path $InstallPath 'vendor\ffmpeg.exe') 'damage'
Remove-Item $Exe
Run-Setup
Assert-File $Exe
Assert-File $Menu
if ((Get-FileHash $Exe).Hash -ne $Hash) { throw 'Repair executable hash differs' }
if ((Get-FileHash (Join-Path $InstallPath 'vendor\ffmpeg.exe')).Hash -ne $EngineHash) { throw 'Repair engine hash differs' }
Assert-File (Join-Path $DataPath 'preservation.txt')
# Same-version repair must also handle a damaged executable and missing app metadata.
Set-Content $Exe 'damaged executable'
Remove-Item 'HKCU:\Software\FileConverter' -Recurse
Run-Setup
if ((Get-FileHash $Exe).Hash -ne $Hash) { throw 'Corrupted executable was not repaired' }
$restored = Get-ItemProperty 'HKCU:\Software\FileConverter'
if ($restored.Version -ne '1.0.5' -or [IO.Path]::GetFullPath($restored.InstallDir).TrimEnd('\') -ne $InstallPath) { throw 'Application metadata was not repaired' }
Assert-File $Menu
Assert-File $Desktop
$diagnostics = Start-Process $Exe -ArgumentList '--diagnostics' -Wait -PassThru
if ($diagnostics.ExitCode -ne 0) { throw 'Installed runtime failed' }
$uninstall = Start-Process (Join-Path $InstallPath 'unins000.exe') -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -Wait -PassThru
if ($uninstall.ExitCode -ne 0) { throw 'Uninstall failed' }
if (Test-Path $Exe) { throw 'Executable remains' }
if (Test-Path $Desktop) { throw 'Desktop shortcut remains' }
if (Test-Path $Menu) { throw 'Start Menu shortcut remains' }
Assert-File (Join-Path $DataPath 'preservation.txt')
Assert-File $UserOutput
Run-Setup
Assert-File $Exe
python "$PSScriptRoot/windows_data_check.py" --verify
if ($LASTEXITCODE -ne 0) { throw 'User settings, presets or queued jobs were not preserved' }
$report = @{
    install = $true; repair_missing_executable = $true; repair_corrupt_executable = $true
    repair_dependency = $true; repair_shortcuts = $true; repair_metadata = $true
    uninstall = $true; reinstall = $true; user_data_preserved = $true; outputs_preserved = $true
    upgrade = $UpgradeVerified
}
$report | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $PSScriptRoot '../artifacts/windows-installer-verification.json')
Write-Output 'Install, missing/corrupt executable and dependency repair, shortcuts/metadata, uninstall, reinstall and data preservation passed.'
Write-Output 'Visual taskbar, DPI/accessibility and interactive maintenance checks still require manual review.'
