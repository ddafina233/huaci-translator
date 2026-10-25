#define MyAppName "划词翻译"
#define MyAppVersion "1.2.0"
#define MyAppExeName "划词翻译.exe"
#define MyAppPublisher "划词翻译 contributors"

[Setup]
AppId={{267425ED-422F-451C-B20E-B95BFFD84FEE}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion=1.2.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=划词翻译安装程序
DefaultDirName={localappdata}\Programs\划词翻译
DefaultGroupName=划词翻译
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=installer
OutputBaseFilename=划词翻译-安装包
SetupIconFile=..\图标.ico
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
AppMutex=Local\HuaCiFanYi.SingleInstance
UsePreviousAppDir=yes
Uninstallable=yes
CreateUninstallRegKey=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "dist\划词翻译\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\安装说明.md"; DestDir: "{app}"; DestName: "使用说明.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\划词翻译"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载划词翻译"; Filename: "{uninstallexe}"
Name: "{autodesktop}\划词翻译"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动划词翻译"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "划词翻译"; Flags: uninsdeletevalue

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /f /im ""{#MyAppExeName}"" >nul 2>nul"; Flags: runhidden; RunOnceId: "KillApp"
