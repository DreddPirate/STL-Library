; Inno Setup 6 script for STL Library
; Build with build_installer.bat (or open this file in Inno Setup and press Compile).

[Setup]
AppName=STL Library
AppVersion=1.0
AppPublisher=STL Library
DefaultDirName={autopf}\STL Library
DefaultGroupName=STL Library
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=STL-Library-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\STL Library.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Installs for the current user without needing administrator rights;
; the wizard offers "install for all users" as an option.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\STL Library\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\STL Library"; Filename: "{app}\STL Library.exe"
Name: "{group}\Uninstall STL Library"; Filename: "{uninstallexe}"
Name: "{autodesktop}\STL Library"; Filename: "{app}\STL Library.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\STL Library.exe"; Description: "Launch STL Library"; Flags: nowait postinstall skipifsilent

[Code]
var
  CachePage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  { Extra wizard page: where to keep thumbnails and spin previews (any drive) }
  CachePage := CreateInputDirPage(wpSelectDir,
    'Thumbnail cache',
    'Where should thumbnails be stored?',
    'Choose a folder on any drive, including an external drive. You can change this later in the app under Settings.',
    False, '');
  CachePage.Add('');
  CachePage.Values[0] := ExpandConstant('{userappdata}\STLLibrary\cache');
end;

function JsonEscape(const S: String): String;
var
  R: String;
begin
  R := S;
  StringChangeEx(R, '\', '\\', True);
  Result := R;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  CfgDir, CfgFile, Json: String;
begin
  if CurStep = ssPostInstall then
  begin
    CfgDir := ExpandConstant('{userappdata}\STLLibrary');
    CfgFile := CfgDir + '\config.json';
    { Only write the setting on a fresh install; never overwrite an existing config }
    if not FileExists(CfgFile) then
    begin
      ForceDirectories(CfgDir);
      Json := '{' + #13#10 + '  "cache_folder": "' + JsonEscape(CachePage.Values[0]) + '"' + #13#10 + '}';
      SaveStringToFile(CfgFile, Json, False);
    end;
  end;
end;
