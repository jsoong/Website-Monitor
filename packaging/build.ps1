<#
.SYNOPSIS
  Build the PageWatch installer and portable zip on Windows.

.DESCRIPTION
  Needs: Windows 10/11 x64, uv (https://docs.astral.sh/uv/), and Inno Setup 6.3 or later
  (`winget install JRSoftware.InnoSetup`). Everything else is installed by `uv sync`.

  Writes to dist\ :
    PageWatch\                          the one-folder build (what the installer packs)
    PageWatch-Setup-<version>.exe       the installer, plus .sha256
    PageWatch-<version>-portable.zip    the portable copy, plus .sha256

  A thin wrapper: all the work is in packaging\build.py, which also runs on Linux (without the
  installer) for CI. This script was written without a Windows machine to run it on.

.PARAMETER NoUi
  Leave out the UI and PySide6: engine and CLI only. Fast, for checking the packaging.

.PARAMETER Smoke
  Run packaging\smoke.py on the finished build (starts the programs, checks a page, stops them).

.PARAMETER SkipInstaller
  Build the folder and the portable zip only (no Inno Setup needed).
#>
[CmdletBinding()]
param(
    [switch]$NoUi,
    [switch]$Smoke,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not installed. See https://docs.astral.sh/uv/"
}

uv sync --extra ui
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

$buildArgs = @("run", "python", "packaging\build.py", "--portable")
if (-not $SkipInstaller) { $buildArgs += "--installer" }
if ($NoUi) { $buildArgs += "--no-ui" }
if ($Smoke) { $buildArgs += "--smoke" }

uv @buildArgs
if ($LASTEXITCODE -ne 0) { throw "the build failed (see above)" }
