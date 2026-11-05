# Packaging PageWatch

PageWatch ships as one folder with three programs, built by PyInstaller, wrapped by an Inno Setup
installer, and also as a portable zip. No Python is needed on the user's machine.

```
dist/PageWatch/
    pagewatch-engine.exe    always on, windowed (no console window), starts at sign-in
    pagewatch-ui.exe        the desktop client, windowed; carries PySide6 and QtWebEngine
    pagewatch-cli.exe       the command line, console
    USER_GUIDE.md           from docs/
    _internal/              the shared Python runtime, libraries, data files, Playwright's driver
```

## What is in this folder

| File | Role |
| --- | --- |
| `bundle.py` | What goes in the bundle (programs, data files, hidden imports), as plain Python so tests can check it |
| `pagewatch.spec` | The PyInstaller spec; reads `bundle.py` |
| `entry/{engine,ui,cli}.py` | The three launchers PyInstaller builds from; each is `launch("<name>")` |
| `build.py` | The one build entry point: bundle, smoke test, portable zip, installer, checksums |
| `build.ps1` | Windows wrapper: `uv sync`, then `build.py --installer --portable` |
| `installer.iss` | The Inno Setup script |
| `smoke.py` | Runs a built or installed folder: engine, a real check, CLI, browser, UI, stop, restart |
| `verify-install.ps1` | The Windows VM acceptance run: install, autostart, upgrade, uninstall |
| `portable/` | `Start PageWatch.cmd` and a readme, added to the portable zip |
| `assets/pagewatch.ico` | The icon, from `tools/make_icon.py` |

This folder is called `packaging` because the spec names it. It must **not** get an
`__init__.py`: as a package it would shadow PyPI's `packaging`, which pytest and pip import. The
scripts load each other by path.

## Build

PyInstaller builds for the platform it runs on, so the Windows installer is built on Windows.

On Windows (Python 3.12 via `uv`, Inno Setup 6.3+ from `winget install JRSoftware.InnoSetup`):

```powershell
.\packaging\build.ps1                    # dist\PageWatch-Setup-<version>.exe and the portable zip
.\packaging\build.ps1 -Smoke             # ... and run the smoke test on the result first
.\packaging\build.ps1 -NoUi -SkipInstaller   # engine + CLI only, no Inno Setup: a quick check
```

On Linux or macOS (CI, or to check the packaging without a Windows machine):

```bash
uv sync --extra ui
uv run python packaging/build.py --smoke --portable     # dist/PageWatch + zip, tested
uv run python packaging/build.py --no-ui --smoke        # about a minute
```

The version is `pagewatch.__version__` (`src/pagewatch/__init__.py`, kept equal to
`pyproject.toml` by a test). It flows into the installer's file name, the programs' Windows
version resources and the portable zip.

## Verify

1. **Always (the normal test run):** `uv run pytest tests/packaging` checks the spec, the bundle
   contents, the launchers, the installer script's invariants, the build commands, the upgrade
   path and portable mode. No build is needed.
2. **Before a release, anywhere:** `uv run pytest -m packaging` builds the real bundle (headless,
   then with the UI) and runs `smoke.py` on it, and on the unpacked portable zip.
3. **Before a release, on a clean Windows 10/11 VM:**
   ```powershell
   .\packaging\verify-install.ps1 -OldInstaller .\PageWatch-Setup-0.1.0.exe -NewInstaller .\PageWatch-Setup-0.2.0.exe
   ```
4. **By hand, on that VM, once per release** (a script cannot do these):
   - Sign out and in. `pagewatch-engine.exe` is running again without anything being clicked.
   - The tray icon is there; its menu works; **Quit engine** stops it; the UI offers to start it.
   - Add a real page; change it; a toast arrives; **Highlighted** shows the change.
   - A page that needs JavaScript is switched to the browser and renders (Edge is used; with Edge
     unavailable the Chromium fallback downloads on first use).
   - Close the lid, reopen: overdue checks catch up (M5).
   - Windows Defender and SmartScreen: note what they say about the unsigned build.
   - Uninstall from Settings > Apps: the task is gone, and the data folder asks before going.

## How the pieces fit

- **Frozen start-up.** `pagewatch.engine.frozen.launch()` is the only thing a launcher does. It
  calls `multiprocessing.freeze_support()` *first*: the engine's worker pool uses `spawn`, and in
  a bundle each worker is a fresh copy of the same executable. It then repairs the missing
  standard streams of a windowed build and runs the program's `main()`.
- **Children are started through `component_command()`** (UI starts the engine, the tray opens the
  UI) and `playwright_install_command()` (installing the fallback Chromium), never through
  `sys.executable -m ...`, which from a bundle would be the wrong program. A test fails if code
  reintroduces that.
- **Data files** are everything non-code under `src/pagewatch`, found by walking the tree, so what
  later milestones add ships without anyone editing the spec. **Metadata** (`.dist-info`) of
  `pagewatch` and its whole dependency tree is copied because libraries read their own version at
  import (aioftp does). **Playwright's Node driver** is collected explicitly: it is how the engine
  drives Edge, and PyInstaller has no hook for it.
- **Plugins** (a later milestone) are loaded from the data folder and may import any standard
  library module, so the public standard library is forced into the bundle.
- **Upgrades.** The installer stops the engine, copies the new files over the old, registers the
  task again (replacing it) and restarts the engine. The data folder is never touched by Setup.
  The new engine migrates it at start-up, after `migrate()` has saved
  `backups/pre-migration-v<N>-<time>.db`; a migration that fails rolls back; an older build
  refuses newer data. All of that is proved by `tests/packaging/test_upgrade.py`, including on a
  frozen data folder written by release 0.1.0.
- **Uninstall** removes the task and the program files and asks before touching the data folder
  (the default answer, and any silent uninstall, keep it).
- **Never change** `AppGuid` in `installer.iss`: it is how Windows recognises an upgrade.

## Releasing

1. Bump the version in `src/pagewatch/__init__.py` and `pyproject.toml` (a test checks they agree).
2. `uv run pytest`, then `uv run pytest -m packaging`.
3. On Windows: `.\packaging\build.ps1 -Smoke`.
4. Optional but recommended: code-sign `dist\PageWatch\*.exe` and then the installer with
   `signtool` (SmartScreen and Defender treat unsigned programs with suspicion). The build does
   not sign; sign between the PyInstaller step and Inno Setup if you add it.
5. Run `verify-install.ps1` on a clean VM, then the manual checks above.
6. Publish `PageWatch-Setup-<version>.exe`, the portable zip and their `.sha256` files.

## Known limits

- Never run on Windows by the author of this folder: see `docs/DECISIONS.md` (M8) for exactly what
  was and was not executed.
- The installer still stops the engine with a hard kill (`schtasks /End`, `taskkill /F /T`). The
  engine can now be asked to quit cleanly (`POST /shutdown`, or `pagewatch-cli stop`, which waits
  until the process has exited and succeeds if none is running), but `StopEngine` in `installer.iss`
  has not been changed to use it: that is Inno Script and could not be compiled or run where this
  was written. To finish it, call `RunCli('stop --wait 30')` there before the hard kill and keep the
  hard kill as the fallback (the old CLI in an upgrade may not have `stop`). The hard kill is safe by
  design (WAL, one transaction per check, interrupted checks re-queued). See `docs/DECISIONS.md`,
  *Merging M6, M7 and M8*.
- Size: roughly 0.3 GB without the UI and 0.9 GB with it, on disk. Playwright's Node driver
  (about 0.13 GB) and QtWebEngine are most of it.
