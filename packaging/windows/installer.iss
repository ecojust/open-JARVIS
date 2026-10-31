; Inno Setup 6. No Python or administrator privileges required at runtime.
#ifndef AppVersion
  #error AppVersion must be supplied by build_release.py
#endif
#ifndef SourceDir
  #error SourceDir must be supplied by build_release.py
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by build_release.py
#endif
#ifndef OutputName
  #error OutputName must be supplied by build_release.py
#endif

[Setup]
AppId={{B6B0DA41-41DA-4A63-8B6C-4AAE1CBCE54E}
AppName=Open JARVIS
AppVersion={#AppVersion}
AppPublisher=ecojust
AppPublisherURL=https://github.com/ecojust/open-JARVIS
DefaultDirName={localappdata}\Programs\Open JARVIS
DefaultGroupName=Open JARVIS
UninstallDisplayIcon={app}\open-JARVIS.exe
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Open JARVIS"; Filename: "{app}\open-JARVIS.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Open JARVIS"; Filename: "{app}\open-JARVIS.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式"; Flags: unchecked

[Run]
Filename: "{app}\open-JARVIS.exe"; Description: "启动 Open JARVIS"; Flags: nowait postinstall skipifsilent

; User models, SQLite databases and workspace files are outside {app}.
; Deliberately no [UninstallDelete] rule touching those directories.
