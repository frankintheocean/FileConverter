#define ProductVersion "1.0.6"
#define AppGuid "{A6B86057-4065-44EC-B12E-EC867BE5C486}"

[Setup]
AppId={{A6B86057-4065-44EC-B12E-EC867BE5C486}
AppName=FileConverter
AppVersion={#ProductVersion}
AppPublisher=FileConverter Contributors
AppCopyright=GPL-3.0 FileConverter Contributors
DefaultDirName={localappdata}\Programs\FileConverter
DefaultGroupName=FileConverter
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\artifacts
OutputBaseFilename=FileConverter-{#ProductVersion}-Windows-x64-Setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\FileConverter.exe
UninstallDisplayName=FileConverter
WizardStyle=modern
WizardResizable=yes
DisableProgramGroupPage=yes
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
LicenseFile=..\LICENSE
VersionInfoVersion={#ProductVersion}
VersionInfoProductName=FileConverter
VersionInfoDescription=FileConverter Installer
ChangesAssociations=no

[Tasks]
Name: desktopicon; Description: "Create a Desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked; Check: DesktopOffered

[Files]
Source: "..\dist\FileConverter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\FileConverter"; Filename: "{app}\FileConverter.exe"; IconFilename: "{app}\FileConverter.exe"; AppUserModelID: "FileConverter.Desktop.1"
Name: "{autodesktop}\FileConverter"; Filename: "{app}\FileConverter.exe"; IconFilename: "{app}\FileConverter.exe"; Tasks: desktopicon; AppUserModelID: "FileConverter.Desktop.1"
Name: "{group}\Uninstall FileConverter"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\FileConverter"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\FileConverter"; ValueType: string; ValueName: "Version"; ValueData: "{#ProductVersion}"
Root: HKCU; Subkey: "Software\FileConverter"; ValueType: dword; ValueName: "DesktopShortcut"; ValueData: "{code:DesktopValue}"

[Run]
Filename: "{app}\FileConverter.exe"; Description: "Launch FileConverter"; Flags: nowait postinstall skipifsilent

[Code]
var
  MaintenancePage: TInputOptionWizardPage;
  ExistingDir: String;
  ExistingVersion: String;
  RemoveUserData: Boolean;
  ExistingDesktop: Cardinal;

function DesktopOffered(): Boolean;
begin
  Result := True;
end;

function DesktopValue(Param: String): String;
begin
  if WizardIsTaskSelected('desktopicon') then Result := '1' else Result := '0';
end;

function CloseInstalledApplication(Directory: String): Boolean;
var
  Code: Integer;
begin
  Result := not CheckForMutexes('Local\FileConverter.Desktop.1');
  if FileExists(Directory + '\FileConverter.exe') then
  begin
    Result := Exec(Directory + '\FileConverter.exe', '--maintenance-close', Directory,
                   SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
    // A damaged executable must be repairable when the application is not running.
    if not Result then Result := not CheckForMutexes('Local\FileConverter.Desktop.1');
  end;
end;

procedure InitializeWizard();
begin
  RegQueryStringValue(HKCU, 'Software\FileConverter', 'InstallDir', ExistingDir);
  RegQueryStringValue(HKCU, 'Software\FileConverter', 'Version', ExistingVersion);
  if ExistingDir = '' then
    RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1', 'InstallLocation', ExistingDir);
  if ExistingVersion = '' then
    RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1', 'DisplayVersion', ExistingVersion);
  if not RegQueryDWordValue(HKCU, 'Software\FileConverter', 'DesktopShortcut', ExistingDesktop) then
  begin
    if FileExists(ExpandConstant('{autodesktop}\FileConverter.lnk')) then ExistingDesktop := 1;
  end;
  if ExistingDir <> '' then WizardForm.DirEdit.Text := ExistingDir;
  if ExistingVersion = '{#ProductVersion}' then
  begin
    MaintenancePage := CreateInputOptionPage(wpWelcome, 'Maintain FileConverter',
      'Repair or uninstall this version',
      'Repair restores application files, conversion engines, shortcuts and registration. Settings, history and converted files are preserved.', True, False);
    MaintenancePage.Add('Repair installation');
    MaintenancePage.Add('Uninstall FileConverter');
    MaintenancePage.SelectedValueIndex := 0;
    if ExistingDesktop = 1 then WizardSelectTasks('desktopicon');
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Code: Integer;
begin
  Result := True;
  if Assigned(MaintenancePage) and (CurPageID = MaintenancePage.ID) and
     (MaintenancePage.SelectedValueIndex = 1) then
  begin
    if FileExists(ExistingDir + '\unins000.exe') then
    begin
      if Exec(ExistingDir + '\unins000.exe', '', ExistingDir, SW_SHOW,
              ewWaitUntilTerminated, Code) then
      begin
        WizardForm.Close;
        Result := False;
      end
      else
        MsgBox('Uninstaller could not start. Use Repair to restore the installation first.', mbError, MB_OK);
    end
    else
      MsgBox('Uninstaller is missing. Use Repair to restore it, then uninstall normally.', mbError, MB_OK);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseInstalledApplication(ExistingDir) then
    Result := 'FileConverter could not close safely. Close it from its window or system tray and try again.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseInstalledApplication(ExpandConstant('{app}'));
  if not Result then
  begin
    MsgBox('Close FileConverter from its window or system tray, then retry uninstall.', mbError, MB_OK);
    exit;
  end;
  RemoveUserData := False;
  if not UninstallSilent then
    RemoveUserData := MsgBox('Also remove application settings, presets and history? Converted output files are always kept. Choose No to preserve your data.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Index: Integer;
begin
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
  begin
    DataDir := ExpandConstant('{localappdata}\FileConverter');
    DeleteFile(DataDir + '\state.sqlite3');
    DeleteFile(DataDir + '\state.sqlite3-wal');
    DeleteFile(DataDir + '\state.sqlite3-shm');
    DeleteFile(DataDir + '\app.lock');
    DeleteFile(DataDir + '\dependency-report.json');
    DeleteFile(DataDir + '\diagnostics.log');
    for Index := 1 to 3 do DeleteFile(DataDir + '\diagnostics.log.' + IntToStr(Index));
    // Remove only an empty data directory; outputs placed here by a user survive.
    RemoveDir(DataDir);
  end;
end;
