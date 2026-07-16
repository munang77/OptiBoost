; OptiBoost 정식 설치 마법사 (Inno Setup 스크립트)
; 로컬 빌드: Inno Setup 설치 후  iscc OptiBoost.iss
; (dist\OptiBoost.exe 를 먼저 빌드해 두어야 함)

#define AppName "OptiBoost"
#define AppVersion "2.0"
#define AppPublisher "munang77"
#define AppURL "https://github.com/munang77/OptiBoost"
#define AppExe "OptiBoost.exe"

[Setup]
AppId={{8F3A1C2E-5B7D-4E9A-A1C3-2D4F6E8B0A12}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputBaseFilename=OptiBoost_Setup
OutputDir=dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=icon.ico

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Windows 시작 시 자동 실행 (트레이)"; GroupDescription: "옵션:"; Flags: unchecked

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "사용법.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; 자동 시작 예약 작업 등록 (최고 권한, 로그온 시)
Filename: "schtasks"; Parameters: "/create /tn OptiBoost_Autostart /tr ""'{app}\{#AppExe}' --minimized"" /sc onlogon /rl highest /f"; Flags: runhidden; Tasks: autostart
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,OptiBoost}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "schtasks"; Parameters: "/delete /tn OptiBoost_Autostart /f"; Flags: runhidden; RunOnceId: "DelAutostart"
Filename: "schtasks"; Parameters: "/delete /tn PCOptimizer_WeeklyClean /f"; Flags: runhidden; RunOnceId: "DelWeekly"
