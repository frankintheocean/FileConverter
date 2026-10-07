param([string]$Installer = "$PSScriptRoot\..\artifacts\FileConverter-1.0.1-Windows-x64-Setup.exe")
$ErrorActionPreference = 'Stop'
$InstallPath = Join-Path $env:LOCALAPPDATA 'Programs\FileConverter'
$DataPath = Join-Path $env:LOCALAPPDATA 'FileConverter'
if (Test-Path $InstallPath) { throw 'Use a clean disposable VM; an installation already exists.' }
function Run-Setup {
    $process = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=desktopicon' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Setup failed: $($process.ExitCode)" }
}
function Assert-File([string]$Path) { if (!(Test-Path $Path)) { throw "Missing: $Path" } }
Run-Setup
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
Write-Output 'Clean install, repair file/engine/shortcut, uninstall, reinstall and data preservation passed.'
Write-Output 'Interactive icon, taskbar, wizard maintenance and upgrade validation still require manual checks.'
