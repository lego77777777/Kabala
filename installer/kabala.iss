; Kabala Windows installer (Inno Setup 6).
; Build:  .venv\Scripts\python build_installer.py   (runs PyInstaller first, then ISCC)
; Per-user install by default: no administrator rights needed.

#define AppName "Kabala"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppPublisher "Kabala"
#define AppURL "https://serewf191-tech.github.io/kabala/"
#define AppExe "Kabala.exe"

[Setup]
AppId={{6F3B1C52-8E4A-4D6B-9C1E-2A7D5B8F4E10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Kabala-Setup-{#AppVersion}
SetupIconFile=..\build\kabala.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "hebrew"; MessagesFile: "compiler:Languages\Hebrew.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Kabala\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; kabala:// deep links (the browser returns here after a PayPal payment)
Root: HKCU; Subkey: "Software\Classes\kabala"; ValueType: string; ValueName: ""; ValueData: "URL:Kabala"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\kabala"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\kabala\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the "start with Windows" entry if the user enabled it. The user's receipts folder is never touched.
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v Kabala /f"; Flags: runhidden; RunOnceId: "RemoveRunKey"
