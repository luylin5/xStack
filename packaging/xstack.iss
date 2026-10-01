; AppVer comes from xstack_version.py; packaging/build.ps1 passes /DAppVer=...
#ifndef AppVer
  #error Build with packaging/build.ps1 so the version is defined.
#endif

[Setup]
AppId={{A711A197-1521-4D71-9895-B79C0EC659A4}
AppName=xStack
AppVersion={#AppVer}
AppPublisher=Yu-Lin Lu
AppPublisherURL=https://orcid.org/0000-0001-9846-8127
DefaultDirName={localappdata}\Programs\xStack
DefaultGroupName=xStack
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release\{#AppVer}
OutputBaseFilename=xStack-{#AppVer}-Setup
SetupIconFile=..\xStack.ico
UninstallDisplayIcon={app}\xStack.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVer}.0.0

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\release\{#AppVer}\xStack-{#AppVer}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\xStack"; Filename: "{app}\xStack.exe"
Name: "{autodesktop}\xStack"; Filename: "{app}\xStack.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\xStack.exe"; Description: "Launch xStack"; Flags: nowait postinstall skipifsilent
