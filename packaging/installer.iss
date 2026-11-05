; PageWatch installer: Inno Setup 6.3 or later.
;
; Compile with `uv run python packaging\build.py --installer` (it passes the version and the
; folder PyInstaller produced), or by hand:
;
;   ISCC.exe /DAppVersion=0.1.0 /DSourceDir=..\dist\PageWatch packaging\installer.iss
;
; What it does
;   * Installs the one-folder build per user by default (no administrator needed: the engine runs
;     in the user's session and keeps its data under %LOCALAPPDATA%\PageWatch). The user can pick
;     "for all users" in the first dialog.
;   * Offers "Start PageWatch when I sign in" (on by default): runs `pagewatch-cli service
;     install --exe <engine>`, which registers the Task Scheduler task, then starts the engine.
;   * Upgrades in place: stops the engine first (Task Scheduler's task and any engine the UI
;     started), copies the new files over the old, registers the task again and restarts the
;     engine. The data folder is never touched; the engine migrates it at start-up after taking
;     its own backup (backups\pre-migration-v<N>-<time>.db).
;   * Refuses to install an older version over a newer one.
;   * Uninstalls the task and the program files, and keeps the data folder unless the user says
;     otherwise.
;
; This file was written and reviewed without a Windows machine or the Inno Setup compiler. The
; checks in tests/packaging/test_installer_script.py are static; packaging/verify-install.ps1 and
; the checklist in packaging/README.md are what actually exercise it.

#ifndef AppVersion
  #error AppVersion is not defined. Compile through packaging\build.py --installer, or pass /DAppVersion=x.y.z.
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\PageWatch"
#endif

#define AppName "PageWatch"

; NEVER CHANGE THIS. It is how Windows and Inno Setup recognise an existing install as the same
; product, so a new version upgrades it instead of installing next to it. A test pins it.
#define AppGuid "413146B5-7D13-4194-82D8-B6CCFBEF6A15"

; The Task Scheduler task `pagewatch-cli service install` creates (pagewatch.cli.service.TASK_NAME).
#define TaskName "PageWatch Engine"

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=PageWatch-Setup-{#AppVersion}
SetupIconFile={#SourcePath}assets\pagewatch.ico
UninstallDisplayIcon={app}\pagewatch-ui.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Restart Manager closes the UI if it is open; the engine has no window, so [Code] stops it.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start PageWatch when I sign in to Windows (monitoring continues while the window is closed)"; GroupDescription: "Monitoring:"
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\pagewatch-ui.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName} User Guide"; Filename: "{win}\notepad.exe"; Parameters: """{app}\USER_GUIDE.md"""
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\pagewatch-ui.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\pagewatch-ui.exe"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
const
  TaskName = '{#TaskName}';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1';

var
  EngineWasRunning: Boolean;

{ -- processes ------------------------------------------------------------------------ }

function EngineIsRunning(): Boolean;
var
  ResultCode: Integer;
begin
  { `find` exits 0 when it found the name in tasklist's output. }
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq pagewatch-engine.exe" /NH | find /I "pagewatch-engine.exe"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

{ Runs pagewatch-cli as the user who started Setup, not as the administrator account a
  "for all users" install may have elevated to: the task must belong to that user, and so must the
  engine that `stop` talks to. Defined before StopEngine, which uses it (Pascal Script reads top
  to bottom). }
function RunCli(const Params: String): Integer;
var
  ResultCode: Integer;
begin
  if not ExecAsOriginalUser(ExpandConstant('{app}\pagewatch-cli.exe'), Params,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    ResultCode := -1;
  Log('pagewatch-cli ' + Params + ' -> ' + IntToStr(ResultCode));
  Result := ResultCode;
end;

{ Stops every engine of this user: the scheduled one and one the UI started.

  First it asks the engine to quit (`pagewatch-cli stop --wait 30`: finish the running checks,
  close the database, remove the lockfile, and the command returns only once the process has
  exited). That is skipped when no engine is running. It cannot work every time: on a first
  install there is no CLI yet, and the CLI of a release from before `stop` existed does not know
  the command (it exits non-zero, which is logged and ignored). So the hard stop below always
  follows and is the fallback; with the engine already gone it finds nothing to end. The engine's
  design covers a hard stop (WAL, one transaction per check, an interrupted check is re-queued at
  start-up). }
procedure StopEngine();
var
  ResultCode: Integer;
begin
  EngineWasRunning := EngineIsRunning();
  if EngineWasRunning then
    RunCli('stop --wait 30');
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "' + TaskName + '"', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM pagewatch-engine.exe', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);  { let Windows release the files }
end;

{ -- install -------------------------------------------------------------------------- }

{ Setup compares what is installed with what it is about to install. Version strings that are
  not plain numbers (a pre-release) are not compared. }
function InitializeSetup(): Boolean;
var
  Installed, Mine: String;
  InstalledPacked, MinePacked: Int64;
begin
  Result := True;
  Mine := '{#AppVersion}';
  if not (RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Installed) or
          RegQueryStringValue(HKLM, UninstallKey, 'DisplayVersion', Installed)) then
    Exit;
  if StrToVersion(Installed, InstalledPacked) and StrToVersion(Mine, MinePacked) then
    if ComparePackedVersion(InstalledPacked, MinePacked) > 0 then
    begin
      SuppressibleMsgBox('PageWatch ' + Installed + ' is already installed, and this installer is ' +
        'the older version ' + Mine + '. Installing it would downgrade the program, and the data ' +
        'folder may already have been upgraded to a format the older version cannot open. ' +
        'Setup will exit; uninstall PageWatch first if you really mean to go back.',
        mbError, MB_OK, IDOK);
      Result := False;
    end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopEngine();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if WizardIsTaskSelected('autostart') then
  begin
    { `service install` replaces an existing task, so an upgrade re-points it at this install. }
    Code := RunCli('service install --exe "' + ExpandConstant('{app}\pagewatch-engine.exe') + '"');
    if Code = 0 then
      ExecAsOriginalUser(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "' + TaskName + '"', '',
        SW_HIDE, ewNoWait, Code)
    else
      SuppressibleMsgBox('PageWatch is installed, but the task that starts it when you sign in ' +
        'could not be created (code ' + IntToStr(Code) + '). Open PageWatch and choose "Start ' +
        'engine", or run "pagewatch-cli service install" later. Details are in the Setup log.',
        mbInformation, MB_OK, IDOK);
  end
  else
  begin
    { The box was unticked: take away a task an earlier install made. It is fine if there is none. }
    RunCli('service uninstall');
    if EngineWasRunning then
      ExecAsOriginalUser(ExpandConstant('{app}\pagewatch-engine.exe'), '--no-console-log',
        ExpandConstant('{app}'), SW_HIDE, ewNoWait, Code);
  end;
end;

{ -- uninstall ------------------------------------------------------------------------ }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    RunCli('service uninstall');  { while the program files still exist }
    StopEngine();
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\PageWatch');
    { Silent uninstalls always keep the data; so does answering No (the default). }
    if DirExists(DataDir) and not UninstallSilent() then
      if MsgBox('Also delete your PageWatch data (bookmarks, page history and backups) in' + #13#10 +
          DataDir + ' ?' + #13#10 + #13#10 + 'Choose No to keep it for a later reinstall.',
          mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
