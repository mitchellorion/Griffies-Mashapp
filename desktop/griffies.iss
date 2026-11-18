; Inno Setup script for the Griffies Windows installer.
; CI builds it with: iscc /DAppVersion=0.1.0 /DOutName=Griffies-Setup-windows-x64 desktop\griffies.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef OutName
  #define OutName "Griffies-Setup"
#endif

[Setup]
AppId={{6B0E8B3C-6D1C-4E0B-9C6A-5A1F2C7E9D41}
AppName=Griffies
AppVersion={#AppVersion}
AppVerName=Griffies {#AppVersion}
AppPublisher=Ctrl Alt Orion LLC
AppPublisherURL=https://github.com/mitchellorion/Griffies-Mashapp
AppUpdatesURL=https://github.com/mitchellorion/Griffies-Mashapp/releases
DefaultDirName={autopf}\Griffies
DefaultGroupName=Griffies
DisableProgramGroupPage=yes
; Per-user install by default: no admin prompt. Users can still choose all-users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=Output
OutputBaseFilename={#OutName}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Griffies.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Griffies\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Griffies"; Filename: "{app}\Griffies.exe"
Name: "{group}\Uninstall Griffies"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Griffies"; Filename: "{app}\Griffies.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Griffies.exe"; Description: "Launch Griffies"; Flags: nowait postinstall skipifsilent
