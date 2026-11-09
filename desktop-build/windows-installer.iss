; Inno Setup script for the Windows installer. Installs for the current user, so no admin
; prompt. Built by the desktop workflow: ISCC /DVersion=2.1.0 desktop-build\windows-installer.iss
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId=io.github.mdbook.karaqueue
AppName=KaraQueue
AppVersion={#Version}
AppPublisher=KaraQueue
AppPublisherURL=https://github.com/mdbook/karaqueue
DefaultDirName={autopf}\KaraQueue
DefaultGroupName=KaraQueue
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=KaraQueue-Setup-windows-x64
UninstallDisplayIcon={app}\KaraQueue.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"

[Files]
Source: "..\dist\KaraQueue\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\KaraQueue"; Filename: "{app}\KaraQueue.exe"
Name: "{autodesktop}\KaraQueue"; Filename: "{app}\KaraQueue.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KaraQueue.exe"; Description: "Start KaraQueue"; Flags: nowait postinstall skipifsilent
