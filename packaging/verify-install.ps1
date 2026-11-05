<#
.SYNOPSIS
  The M8 acceptance test, for a clean Windows 10/11 VM: install, autostart, upgrade with data
  intact, downgrade refused, uninstall keeping data.

.DESCRIPTION
  Run in an elevated or normal PowerShell on a VM that has no PageWatch yet. It installs
  silently, so nothing is clicked.

    .\verify-install.ps1 -NewInstaller .\PageWatch-Setup-0.2.0.exe -OldInstaller .\PageWatch-Setup-0.1.0.exe

  With only -NewInstaller it checks install and uninstall; with -OldInstaller as well it installs
  the old one, adds data, installs the new one over it and checks the data survived.

  What it cannot do is sign out and in: "starts at logon" is checked by finding the logon-trigger
  task, stopping the engine and having Task Scheduler start it again. Do the real sign-out and
  sign-in once by hand (packaging\README.md, "Manual checks").

  Written without a Windows machine to run it on: read it once before trusting a pass or a fail.

.PARAMETER NewInstaller   The installer under test.
.PARAMETER OldInstaller   An earlier installer, to test the upgrade.
.PARAMETER DataDir        Where the engine keeps its data (default %LOCALAPPDATA%\PageWatch).
.PARAMETER InstallDir     Where the installer puts the programs (default: the per-user folder).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$NewInstaller,
    [string]$OldInstaller,
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "PageWatch"),
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\PageWatch")
)

$ErrorActionPreference = "Stop"
$TaskName = "PageWatch Engine"
$results = New-Object System.Collections.Generic.List[object]

function Check([string]$Name, [scriptblock]$Test) {
    try { $detail = & $Test; $ok = $true } catch { $detail = $_.Exception.Message; $ok = $false }
    $results.Add([pscustomobject]@{ Name = $Name; Ok = $ok; Detail = "$detail" })
    $mark = if ($ok) { "ok  " } else { "FAIL" }
    Write-Host ("{0} {1} {2}" -f $mark, $Name, $detail)
}

function Wait-For([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "timed out after $Seconds s waiting for $What"
}

function Version-Of([string]$InstallerPath) {
    if ((Split-Path $InstallerPath -Leaf) -match 'Setup-(\d+(\.\d+)*)') { return $Matches[1] }
    throw "cannot read the version from the installer's file name: $InstallerPath"
}

function Run-Installer([string]$Path) {
    $log = Join-Path $env:TEMP ("pagewatch-setup-" + [IO.Path]::GetFileNameWithoutExtension($Path) + ".log")
    $p = Start-Process -FilePath $Path -Wait -PassThru -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$log", "/DIR=$InstallDir")
    return $p.ExitCode
}

function Engine-Running { return [bool](Get-Process -Name "pagewatch-engine" -ErrorAction SilentlyContinue) }
function Lock-Info {
    $lock = Join-Path $DataDir "engine.lock"
    if (-not (Test-Path $lock)) { return $null }
    return Get-Content $lock -Raw | ConvertFrom-Json
}
function Cli { & (Join-Path $InstallDir "pagewatch-cli.exe") @args }

$newVersion = Version-Of $NewInstaller
$firstInstaller = if ($OldInstaller) { $OldInstaller } else { $NewInstaller }
$firstVersion = Version-Of $firstInstaller

Check "starting point: no PageWatch installed" {
    if (Test-Path (Join-Path $InstallDir "pagewatch-engine.exe")) { throw "already installed in $InstallDir" }
    if (Engine-Running) { throw "an engine is already running" }
    "clean"
}

# -- install ------------------------------------------------------------------------------------

Check "install $firstVersion silently" {
    $code = Run-Installer $firstInstaller
    if ($code -ne 0) { throw "installer exit code $code" }
    "exit code 0"
}
Check "the three programs are installed" {
    foreach ($n in "pagewatch-engine.exe", "pagewatch-ui.exe", "pagewatch-cli.exe", "USER_GUIDE.md") {
        if (-not (Test-Path (Join-Path $InstallDir $n))) { throw "missing $n" }
    }
    $InstallDir
}
Check "the start-at-sign-in task is registered as the spec describes" {
    $xml = (schtasks /Query /TN $TaskName /XML | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "schtasks /Query failed: task '$TaskName' not found" }
    foreach ($needle in "<LogonTrigger>", "pagewatch-engine.exe", "<RestartOnFailure>",
        "<DisallowStartIfOnBatteries>false", "<MultipleInstancesPolicy>IgnoreNew", "<ExecutionTimeLimit>PT0S") {
        if ($xml -notmatch [regex]::Escape($needle)) { throw "task XML lacks $needle" }
    }
    "logon trigger, restart on failure, allowed on battery, one instance, no time limit"
}
Check "the engine is running after the install" {
    Wait-For { (Lock-Info) -and (Engine-Running) } 90 "the engine to start"
    "pid $((Lock-Info).pid), version $((Lock-Info).version)"
}
Check "the engine reports the version that was installed" {
    if ((Lock-Info).version -ne $firstVersion) { throw "engine $((Lock-Info).version), installed $firstVersion" }
    $firstVersion
}

# -- autostart: Task Scheduler brings the engine back ------------------------------------------

Check "Task Scheduler starts the engine again after it is stopped" {
    Get-Process -Name "pagewatch-engine" -ErrorAction SilentlyContinue | Stop-Process -Force
    Wait-For { -not (Engine-Running) } 30 "the engine to stop"
    Remove-Item (Join-Path $DataDir "engine.lock") -ErrorAction SilentlyContinue
    schtasks /Run /TN $TaskName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks /Run failed" }
    Wait-For { (Lock-Info) -and (Engine-Running) } 90 "the task to start the engine"
    "engine up again (a real sign-out/sign-in is a manual check)"
}

# -- data, then the upgrade -------------------------------------------------------------------------

Check "add a bookmark through the CLI" {
    Cli add "http://127.0.0.1:9/verify-install" --name "verify-install" --interval 1d | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "pagewatch-cli add failed" }
    $names = @(Cli --json list | ConvertFrom-Json | ForEach-Object { $_.name })
    if ($names -notcontains "verify-install") { throw "the bookmark is not listed" }
    "listed"
}
$backupsBefore = @(Get-ChildItem (Join-Path $DataDir "backups") -Filter "pre-migration-*.db" -ErrorAction SilentlyContinue).Count

if ($OldInstaller) {
    Check "install $newVersion over $firstVersion (the upgrade)" {
        $code = Run-Installer $NewInstaller
        if ($code -ne 0) { throw "installer exit code $code" }
        "exit code 0"
    }
    Check "the engine was restarted by the upgrade, on the new version" {
        Wait-For { (Lock-Info) -and (Engine-Running) -and ((Lock-Info).version -eq $newVersion) } 90 "the new engine"
        "version $((Lock-Info).version)"
    }
    Check "the data survived: the bookmark is still there" {
        $names = @(Cli --json list | ConvertFrom-Json | ForEach-Object { $_.name })
        if ($names -notcontains "verify-install") { throw "the bookmark is gone after the upgrade" }
        "still listed"
    }
    Check "the pre-migration backup, if the schema changed" {
        $now = @(Get-ChildItem (Join-Path $DataDir "backups") -Filter "pre-migration-*.db" -ErrorAction SilentlyContinue).Count
        "$($now - $backupsBefore) new (expected 1 if $newVersion changed the database schema, else 0)"
    }
    Check "the task still points at this install" {
        $xml = (schtasks /Query /TN $TaskName /XML | Out-String)
        if ($xml -notmatch [regex]::Escape($InstallDir)) { throw "the task does not point into $InstallDir" }
        "yes"
    }
    Check "installing the older version over the newer one is refused" {
        $code = Run-Installer $OldInstaller
        if ($code -eq 0) { throw "the installer agreed to downgrade" }
        if ((Lock-Info).version -ne $newVersion) { throw "the installed version changed" }
        "refused with exit code $code; still $newVersion"
    }
}

# -- uninstall ---------------------------------------------------------------------------------

Check "uninstall silently" {
    $unins = Join-Path $InstallDir "unins000.exe"
    if (-not (Test-Path $unins)) { throw "no uninstaller at $unins" }
    $p = Start-Process -FilePath $unins -Wait -PassThru -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    if ($p.ExitCode -ne 0) { throw "uninstaller exit code $($p.ExitCode)" }
    "exit code 0"
}
Check "the task, the engine and the program files are gone" {
    schtasks /Query /TN $TaskName 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "the task is still registered" }
    if (Engine-Running) { throw "an engine is still running" }
    if (Test-Path (Join-Path $InstallDir "pagewatch-engine.exe")) { throw "program files remain" }
    "removed"
}
Check "the data folder was kept" {
    if (-not (Test-Path (Join-Path $DataDir "pagewatch.db"))) { throw "pagewatch.db is gone" }
    $DataDir
}

# -- summary -----------------------------------------------------------------------------------

$failed = @($results | Where-Object { -not $_.Ok })
Write-Host ""
Write-Host ("{0} checks, {1} failed" -f $results.Count, $failed.Count)
Write-Host "Your test data is still in $DataDir; delete it to start over."
if ($failed.Count -gt 0) { exit 1 }
exit 0
