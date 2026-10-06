; ─────────────────────────────────────────────────────────────────────────────
; SHUNGITE — Inno Setup installer script
; Builds: dist\Shungite_Setup_v<version>.exe  (MSI-Afterburner-style wizard)
;
; PREREQUISITE for local testing: build the app first —
;   cd D:\PEAK_Master
;   build_venv\Scripts\python -m PyInstaller PEAK_GITHUB_true.spec --noconfirm --clean --distpath dist3 --workpath build3\b
; This .iss expects the PyInstaller onedir output at:
;   $(ROOT)\dist3\Shungite\        (Shungite.exe + _internal\)
; Compile with Inno Setup 6:  ISCC.exe installer\Shungite.iss
; ─────────────────────────────────────────────────────────────────────────────

#define MyAppName "SHUNGITE"
#define MyAppExeName "Shungite.exe"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Irteza Asif"
#define MyAppURL "https://github.com/IrtezaAsif/Shungite"
#define MyAppYear "2026"

[Setup]
AppId={{9F7D2C4A-6B1E-4C5D-9E8A-1D3F5B7C9E11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Program Files install — per-machine shortcuts; no admin needed for the app
; data (that lives in %LOCALAPPDATA%\..\LocalLow\Shungite regardless).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Shungite_Setup_v{#MyAppVersion}
SetupIconFile=shungite.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
LicenseFile=LICENSE.rtf
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
MinVersion=10.0
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "desktopicon\common"; Description: "For all users"; GroupDescription: "Additional shortcuts:"; Flags: exclusive
Name: "desktopicon\user"; Description: "For me only"; GroupDescription: "Additional shortcuts:"; Flags: exclusive unchecked
Name: "quicklaunchicon"; Description: "Add to &Quick Launch"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The entire PyInstaller onedir output — exe + _internal (ffmpeg, deno,
; webview/pythonnet, PYZ archive with yt-dlp/mutagen/titanium_enrich…)
Source: "..\dist3\Shungite\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; App data home — config, cookies, logins, (default) Music Library land here.
; Created at runtime too, but pre-create so the dir exists even before
; first launch, with proper user perms.
Name: "{userappdata}\..\LocalLow\Shungite"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} on GitHub"; Filename: "{#MyAppURL}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{#MyAppName} — launch now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the installed binaries tree on uninstall (user data in LocalLow
; is deliberately KEPT — their downloaded music library lives there).
Type: filesandordirs; Name: "{app}"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nYouTube Music + Spotify downloader with embedded cover art, synced lyrics, genres and YouTube links.%n%nIt is recommended that you close all other applications before continuing.
