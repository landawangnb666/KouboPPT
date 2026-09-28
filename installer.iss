; KouboPPT 安装包脚本（Inno Setup 6）
; 正常从 build.py 调用（会自动带上版本号）：
;   ISCC /DAppVersion=0.4.0 installer.iss
; 用 Inno Setup 图标界面手动编译也可以，版本号取下面的默认值。

#define AppName "口播PPT KouboPPT"
#ifndef AppVersion
  #define AppVersion "0.4.0"
#endif

[Setup]
AppId={{58C52275-7281-468B-9388-5D6310C46C4E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\KouboPPT
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=KouboPPT_Setup_v{#AppVersion}
SetupIconFile=build_assets\icon.ico
UninstallDisplayIcon={app}\KouboPPT.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 免管理员权限、按当前用户安装（不弹 UAC）；静默测试可用 /ALLUSERS 覆盖
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
; 中文语言文件来自社区翻译（kira-96/Inno-Setup-Chinese-Simplified-Translation）
Name: "chinese"; MessagesFile: "build_assets\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\KouboPPT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\KouboPPT.exe"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\KouboPPT.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KouboPPT.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
