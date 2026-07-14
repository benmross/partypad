#define MyAppName "PartyPad"
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist"
#endif

[Setup]
AppId={{147D94C8-B342-4B32-A447-4513DD266DA9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\PartyPad
DefaultGroupName=PartyPad
OutputBaseFilename=PartyPad-{#MyAppVersion}-windows-x64-setup-unsigned
OutputDir={#SourceDir}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE

[Files]
Source: "{#SourceDir}\partypad.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\UNSIGNED-ALPHA.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\PartyPad"; Filename: "{app}\partypad.exe"

[Run]
Filename: "{app}\partypad.exe"; Parameters: "--help"; Description: "Open PartyPad command help"; Flags: postinstall nowait skipifsilent
