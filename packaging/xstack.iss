[Setup]
AppId={{A711A197-1521-4D71-9895-B79C0EC659A4}
AppName=xStack
AppVersion=1.5
AppPublisher=Yu-Lin Lu
AppPublisherURL=https://orcid.org/0000-0001-9846-8127
DefaultDirName={localappdata}\Programs\xStack
DefaultGroupName=xStack
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release\1.5
OutputBaseFilename=xStack-1.5-Setup
SetupIconFile=..\xStack.ico
UninstallDisplayIcon={app}\xStack.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion=1.5.0.0

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\release\1.5\xStack-1.5\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\xStack"; Filename: "{app}\xStack.exe"
Name: "{autodesktop}\xStack"; Filename: "{app}\xStack.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\xStack.exe"; Description: "Launch xStack"; Flags: nowait postinstall skipifsilent
