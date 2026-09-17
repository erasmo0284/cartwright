; Inno Setup script for Cartwright.
;
; Compiled by scripts/build.py --installer, which passes /DMyAppVersion.
;
; Design decisions:
;
; * Per-user install, no administrator prompt. PrivilegesRequired=lowest with
;   {autopf} puts the application in %LOCALAPPDATA%\Programs, which is
;   writable by the user who is installing. That matters because the app
;   downloads its browser into %LOCALAPPDATA% at first run; a machine-wide
;   install under Program Files would invite permission problems for no gain.
;
; * AppMutex matches the name the application's single-instance guard uses,
;   so upgrading over a running copy asks the user to close it rather than
;   silently failing to replace a locked file.
;
; * Uninstalling asks before deleting the user's data, because that data
;   includes the ~430 MB browser and the signed-in Amazon session. Someone
;   reinstalling should not have to download it again or sign in again.

#define MyAppName "Cartwright"
#define MyAppShortName "Cartwright"
#define MyAppPublisher "erasmo0284"
#define MyAppExeName "Cartwright.exe"
#define MyDataFolder "Cartwright"

#ifndef MyAppVersion
  #define MyAppVersion "1.0.1"
#endif

#define DistDir "..\dist\Cartwright"

[Setup]
; Generated once and never changed: it is what identifies an upgrade as an
; upgrade rather than a second side-by-side installation.
AppId={{7C4A9E21-3F58-4B0D-9E77-2A6C51D8B4F3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup

; --- per-user, never elevates -------------------------------------------
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
AllowNoIcons=yes

; --- 64-bit only ---------------------------------------------------------
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; --- output --------------------------------------------------------------
OutputDir=output
OutputBaseFilename={#MyAppShortName}-{#MyAppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
SetupIconFile=..\build\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
DisableWelcomePage=no
LicenseFile=..\installer\license.txt

; --- upgrading over a running copy --------------------------------------
; Must match BRAND.single_instance_key's mutex name in the application.
AppMutex=Cartwright.SingleInstance
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start {#MyAppName} when I sign in to Windows"; \
    GroupDescription: "Startup"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The Start Menu shortcut also gives Windows an AppUserModelID target, which
; is what lets notifications show the application's own name and icon.
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    AppUserModelID: "Cartwright.Desktop"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    AppUserModelID: "Cartwright.Desktop"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Parameters: "--tray"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only things the application creates inside its own install folder.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
Type: dirifempty; Name: "{app}"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nThe app automates your own Amazon account in its own private browser window. It never stores your Amazon password or your card details.%n%nOn first run it downloads a browser (about 430 MB), which happens once.

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#MyDataFolder}');
end;

function FreeSpaceIsSufficient(): Boolean;
var
  FreeMB, TotalMB: Cardinal;
begin
  Result := True;
  if GetSpaceOnDisk(ExpandConstant('{localappdata}'), True, FreeMB, TotalMB) then
  begin
    { The app itself is modest; the browser it downloads on first run is not. }
    if FreeMB < 1200 then
    begin
      { SuppressibleMsgBox, not MsgBox: a plain MsgBox ignores
        /SUPPRESSMSGBOXES and hangs an unattended install forever waiting for
        a click nobody can give. The Default is what a silent run returns. }
      Result := SuppressibleMsgBox(
        'There is only ' + IntToStr(FreeMB) + ' MB free on this drive.' + #13#10#13#10 +
        '{#MyAppName} needs roughly 1 GB: about 150 MB for the app and about ' +
        '430 MB for the browser it downloads the first time it runs.' + #13#10#13#10 +
        'Install anyway?',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDYES) = IDYES;
    end;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := FreeSpaceIsSufficient();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := DataDir();
    if DirExists(Dir) then
    begin
      { SuppressibleMsgBox with IDNO: a silent uninstall keeps the data, which
        is the recoverable choice, and does not block. }
      if SuppressibleMsgBox(
           'Also delete your {#MyAppName} data?' + #13#10#13#10 +
           'This removes the downloaded browser (about 430 MB), the private ' +
           'browser profile that keeps you signed in to Amazon, your watch ' +
           'list, your history and the log files from:' + #13#10 +
           Dir + #13#10#13#10 +
           'Choose No to keep them, so a reinstall does not need to download ' +
           'the browser or sign in again.' + #13#10#13#10 +
           'Orders already placed on Amazon are not affected either way.',
           mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      begin
        DelTree(Dir, True, True, True);
      end;
    end;

    { The app registers itself for Windows notifications under HKCU. Removing
      it keeps the uninstall clean; it is recreated on next run if the app is
      reinstalled. }
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER,
      'SOFTWARE\Classes\AppUserModelId\Cartwright.Desktop');
    { And the "start with Windows" entry, if the app created one. }
    RegDeleteValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Run', 'Cartwright');
  end;
end;
