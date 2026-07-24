; Inno Setup script for Code Wood (Windows).
; Produces an .exe installer that:
;   - lets the user choose whether to install for the current user or all users
;   - creates two Start Menu shortcuts: the terminal UI (TUI) and the GUI
;   - optionally registers the install directory on the PATH environment variable
;
; This script is driven by build\pack.bat, which passes the version and the
; built one-dir bundle location via /D defines:
;   ISCC.exe /DAppVersion=0.0.1 /DSourceDir=..\dist\codewood /DOutputDir=..\dist build\installer.iss
;
; Defaults below keep the script runnable standalone for quick iteration.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\codewood"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "Code Wood"
#define AppPublisher "Reed Yang"
#define AppExe "codewood.exe"
#define GuiExe "codewood-gui.exe"
; AppId is a stable GUID identifying the product across versions/upgrades.
#define AppId "{{B7B0C7A2-2E6E-4C2B-9C9E-CODEWOOD0001}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\CodeWood
DefaultGroupName={#AppName}
; "auto" install mode: respects the user's per-user / all-users choice made
; on the privileges page below, elevating only when "all users" is selected.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Only embed a custom setup icon when one is present next to this script.
#if FileExists(AddBackslash(SourcePath) + "app_icon.ico")
SetupIconFile=app_icon.ico
#endif
UninstallDisplayIcon={app}\{#AppExe}
OutputDir={#OutputDir}
OutputBaseFilename=CodeWood-{#AppVersion}-windows-x64-setup
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut for the GUI"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "addtopath"; Description: "Add Code Wood to the &PATH environment variable"; GroupDescription: "Environment:"; Flags: unchecked

[Files]
; Copy the entire PyInstaller one-dir bundle (codewood.exe, codewood-gui.exe,
; and the shared _internal\ runtime + resources).
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Start Menu: terminal UI (opens with a console window) + GUI launcher.
Name: "{group}\Code Wood (Terminal)"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Code Wood (GUI)"; Filename: "{app}\{#GuiExe}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
; Optional desktop shortcut for the GUI.
Name: "{autodesktop}\Code Wood"; Filename: "{app}\{#GuiExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#GuiExe}"; Description: "Launch Code Wood (GUI)"; Flags: nowait postinstall skipifsilent

[Code]
const
  EnvironmentKeyHKLM = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  EnvironmentKeyHKCU = 'Environment';

function PathRootKey(): Integer;
begin
  { All-users installs write to the machine-wide PATH; per-user installs write
    to the per-user PATH. IsAdminInstallMode() reflects the privileges choice. }
  if IsAdminInstallMode() then
    Result := HKEY_LOCAL_MACHINE
  else
    Result := HKEY_CURRENT_USER;
end;

function PathSubKey(): String;
begin
  if IsAdminInstallMode() then
    Result := EnvironmentKeyHKLM
  else
    Result := EnvironmentKeyHKCU;
end;

function DirOnPath(const Paths, Dir: String): Boolean;
var
  Hay, Needle: String;
begin
  { Case-insensitive, delimiter-aware containment check to avoid partial and
    duplicate matches (e.g. ...\CodeWood vs ...\CodeWoodX). }
  Hay := ';' + Uppercase(Paths) + ';';
  Needle := ';' + Uppercase(Dir) + ';';
  Result := Pos(Needle, Hay) > 0;
end;

procedure AddDirToPath();
var
  RootKey: Integer;
  SubKey, Existing, NewValue: String;
begin
  RootKey := PathRootKey();
  SubKey := PathSubKey();
  if not RegQueryStringValue(RootKey, SubKey, 'Path', Existing) then
    Existing := '';
  if DirOnPath(Existing, ExpandConstant('{app}')) then
    exit;
  if (Existing <> '') and (Copy(Existing, Length(Existing), 1) <> ';') then
    NewValue := Existing + ';' + ExpandConstant('{app}')
  else
    NewValue := Existing + ExpandConstant('{app}');
  RegWriteStringValue(RootKey, SubKey, 'Path', NewValue);
end;

procedure RemoveDirFromPath();
var
  RootKey: Integer;
  SubKey, Existing, AppDir, Rebuilt, Part: String;
  P: Integer;
begin
  RootKey := PathRootKey();
  SubKey := PathSubKey();
  if not RegQueryStringValue(RootKey, SubKey, 'Path', Existing) then
    exit;
  AppDir := Uppercase(ExpandConstant('{app}'));
  Rebuilt := '';
  Existing := Existing + ';';
  repeat
    P := Pos(';', Existing);
    Part := Copy(Existing, 1, P - 1);
    Existing := Copy(Existing, P + 1, Length(Existing));
    if (Part <> '') and (Uppercase(Part) <> AppDir) then
    begin
      if Rebuilt <> '' then
        Rebuilt := Rebuilt + ';';
      Rebuilt := Rebuilt + Part;
    end;
  until Existing = '';
  RegWriteStringValue(RootKey, SubKey, 'Path', Rebuilt);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('addtopath') then
      AddDirToPath();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Best-effort: always try to remove our dir from PATH on uninstall. }
  if CurUninstallStep = usUninstall then
    RemoveDirFromPath();
end;
