; Inno Setup script for Wackro-Setup-<version>.exe. Build it with:  .\build.ps1 -Installer
; (needs Inno Setup 6 from https://jrsoftware.org/isdl.php). By hand, after the folder build
; (WACKRO_ONEDIR=1 pyinstaller ... --distpath dist\app):  ISCC /DAppVersion=1.7.5 installer\Wackro.iss
;
; What it does:
; * "Install for all users" (Program Files, asks for admin) or "Just for me" (no admin).
; * A page asking where recordings go. Left at Documents\Wackro, nothing is written and every
;   Windows user gets their own Documents\Wackro. Changed, it's saved as install.json next to
;   Wackro.exe (folders.py reads it). Settings > Recordings folder can still change it later.
; * Start menu: Wackro, Wackro (Administrator) (= Wackro.exe --admin, see main.py), Uninstall.
;   Desktop shortcut optional.
; * Wackro must be closed first (its single-instance mutex, single_instance.py).
; * Uninstall asks whether to remove the settings and log too. Recordings are never touched.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Wackro"
#define AppExe "Wackro.exe"

[Setup]
; Never change AppId: it's how Windows knows a new version is an upgrade of the old one.
AppId={{6E0B8F3A-2C4D-4E7A-9B1F-5D8C3A7E2F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Trixianchez
AppPublisherURL=https://github.com/Trixianchez/wackro
AppSupportURL=https://github.com/Trixianchez/wackro/issues
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AppMutex=Local\Wackro
OutputDir=..\dist
OutputBaseFilename=Wackro-Setup-{#AppVersion}
SetupIconFile=..\assets\Wackro.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\app\Wackro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} (Administrator)"; Filename: "{app}\{#AppExe}"; Parameters: "--admin"; Comment: "Wackro with administrator rights, to play into programs running as administrator"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
Type: files; Name: "{app}\install.json"

[Code]
var
  RecordingsPage: TInputDirWizardPage;

function DefaultRecordingsDir(): String;
begin
  Result := ExpandConstant('{userdocs}\Wackro');
end;

procedure InitializeWizard();
begin
  RecordingsPage := CreateInputDirPage(wpSelectDir,
    'Recordings folder', 'Where should Wackro save your recordings?',
    'Save and Open will start in this folder. You can change it any time in Wackro: ' +
    'Settings > Recordings folder.', False, '');
  RecordingsPage.Add('');
  RecordingsPage.Values[0] := GetPreviousData('RecordingsDir', DefaultRecordingsDir());
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'RecordingsDir', RecordingsPage.Values[0]);
end;

function JsonEscape(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  InfoFile: String;
  Lines: TArrayOfString;
begin
  if CurStep <> ssPostInstall then
    exit;
  InfoFile := ExpandConstant('{app}\install.json');
  if CompareText(RemoveBackslashUnlessRoot(RecordingsPage.Values[0]), DefaultRecordingsDir()) = 0 then
  begin
    DeleteFile(InfoFile);   { the default: each user's own Documents\Wackro }
    exit;
  end;
  ForceDirectories(RecordingsPage.Values[0]);
  SetArrayLength(Lines, 1);
  Lines[0] := '{"recordings_dir": "' + JsonEscape(RecordingsPage.Values[0]) + '"}';
  if not SaveStringsToUTF8File(InfoFile, Lines, False) then
    Log('Could not write ' + InfoFile);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\Wackro');
  if UninstallSilent or not DirExists(DataDir) then
    exit;
  if MsgBox('Also remove your Wackro settings and log?' + #13#10#13#10 +
            'Your recordings are kept either way.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
