# Decisions and deviations: M8 (Packaging)

Kept in its own file, beside `docs/DECISIONS.md`, so the sessions building M5, M6 and M7 in
parallel and this one do not collide on it. Fold it into `DECISIONS.md` as an "M8" section after
the merge. `docs/ARCHITECTURE.md` was **not** edited for the same reason; the paragraph to add is
at the end of this file. Checklist: `TASKS_M8.md`. Build, verify and release steps:
`packaging/README.md`.

## What M8 delivers

| SPEC | Delivered |
| --- | --- |
| PyInstaller build | `packaging/pagewatch.spec` + `bundle.py` + `entry/*.py`; one folder, three programs |
| Inno Setup installer | `packaging/installer.iss`: per-user, autostart task, in-place upgrade, downgrade refused, data kept on uninstall |
| Upgrade path with pre-migration backup | The mechanism already existed (`migrate()`); M8 proves it (`tests/packaging/test_upgrade.py`, 9 tests incl. a frozen release-0.1.0 data folder) and makes the installer respect it |
| Short user guide | `docs/USER_GUIDE.md`, shipped in the folder and linked from the Start menu |
| Portable install, multiple instances (appendix) | Portable zip + launcher; side-by-side engines tested with real processes |
| "A clean Windows VM installs, autostarts at logon and survives an upgrade with data intact" | `packaging/verify-install.ps1` + a manual checklist. **Not run** (see below) |

## Decisions and deviations

- **Files outside the spec's layout.** The SPEC lists `packaging/pagewatch.spec installer.iss`.
  Added beside them: `bundle.py` (what goes in the bundle, plain Python so tests can check it
  without PyInstaller), `entry/` (three launchers), `build.py`/`build.ps1`, `smoke.py`,
  `verify-install.ps1`, `portable/`, `assets/`, `README.md`. Also `tools/make_icon.py`,
  `tools/gen_upgrade_fixture.py`, `docs/USER_GUIDE.md`, `.gitattributes` (CRLF for the Windows
  scripts). `packaging/` has no `__init__.py` on purpose: it would shadow PyPI's `packaging`.
- **`pyinstaller` joins the `dev` dependency group** (the SPEC's technology table names
  PyInstaller). Not a separate group: the packaging tests must always run, not skip when someone
  forgot `--group`. It is never in the runtime install. `uv.lock` was regenerated; if it conflicts
  at merge, take either side and run `uv lock`.
- **`engine/frozen.py` is the one place that knows about PyInstaller.** Three places started a
  child with `sys.executable -m ...` (UI starts engine, tray opens UI, browser manager installs
  Chromium), which in a bundle starts the wrong program. Each is now a one-line change calling
  `component_command()` / `playwright_install_command()`. A test
  (`test_nothing_in_the_package_spawns_python_dash_m`) fails if code reintroduces the pattern.
- **`playwright install` runs Playwright's Node driver directly** (`node cli.js install
  chromium`) instead of `python -m playwright install chromium`, in source and in the bundle. Same
  program (`python -m playwright` only forwards to it; both report `Version 1.63.0` here), one
  code path to test. The download itself was not run (the sandbox blocks it).
- **Launchers call `multiprocessing.freeze_support()` first.** The worker pool uses `spawn`; in a
  bundle each worker is a copy of the engine exe started with `--multiprocessing-fork`. Without
  it every worker would start the whole application. The component's `main` is imported only
  afterwards, so a worker does not import the API server.
- **The engine and UI are windowed (`console=False`); the CLI has a console.** The engine starts
  at logon and must not flash a terminal. A windowed program has `sys.stdout/stderr = None`, so
  `launch()` gives it the null device, or the parent's console when it was started from a
  terminal (`AttachConsole`), so `pagewatch-engine --version` still prints. **The `AttachConsole`
  path has only been tested with stand-ins.**
- **Per-user install by default** (`PrivilegesRequired=lowest`, "for all users" selectable): the
  engine runs in the user's session and keeps its data under `%LOCALAPPDATA%`. The post-install
  steps run through `ExecAsOriginalUser`, so with an elevated "all users" install the Task
  Scheduler task still belongs to the user who ran Setup.
- **Autostart is `pagewatch-cli service install --exe <engine>`** (M5's command, built for this),
  on by default, replaced on every upgrade, removed on uninstall or when the box is unticked on an
  upgrade. The installer also starts the engine through the task (`schtasks /Run`), so a first
  install works without signing out. The task name in the script equals M5's `TASK_NAME`
  (tested after the merge, see below).
- **The product GUID is fixed** (`413146B5-7D13-4194-82D8-B6CCFBEF6A15`, generated once, pinned by
  a test). Changing it turns every future upgrade into a second install.
- **Downgrades are refused twice:** by Setup (`InitializeSetup` compares versions) and by the
  engine (`SchemaTooNew` when the data is newer than the build). The second is the one that
  protects data; the first stops a half-downgraded install.
- **The data folder is never deleted unless asked.** No `[UninstallDelete]`; a prompt (default
  No, skipped and treated as No for silent uninstalls) is the only way `DelTree` runs.
- **Metadata is copied for the whole dependency tree** (`copy_metadata("pagewatch",
  recursive=True)`). Found the hard way: `aioftp` reads `importlib.metadata.version("aioftp")` at
  import and the first frozen engine died at start. Keyring (M6) discovers its backends from entry
  points, which needs the same.
- **Data files are everything non-code under `src/pagewatch`**, discovered by walking the tree, so
  M5-M7 additions (report templates, plugin examples, ...) ship without editing the spec. A test
  compares the list with an independent walk.
- **The public standard library is forced into the bundle** (`bundle.stdlib_modules()`). Plugins
  (M7) are "trusted local code" loaded at run time and may import anything; PyInstaller keeps
  only what the application itself imports.
- **Playwright's driver is collected explicitly** (no PyInstaller hook exists). It is 126 MB of
  `node` plus the CLI, and it is needed even for the Edge channel.
- **UPX is off** (slower start-up, antivirus false positives). **No code signing**: no
  certificate; documented as a release step. Expect SmartScreen warnings until it is signed.
- **The installer stops the engine with a hard kill** (`schtasks /End`, `taskkill /F /T`). A
  windowed program receives no console Ctrl+Break and the engine has no API to ask it to quit.
  The crash-safety design covers it (WAL, one transaction per check, an interrupted check is
  re-queued at start-up). See the open questions.
- **Portable mode = zip + launcher.** `Start PageWatch.cmd` points all three programs at `data\`
  next to them and waits for the lockfile before opening the window. No autostart; the readme
  shows how to add it. Needs no code: `--data-dir` already existed on all three entry points.
- **`-m packaging` is opt-in** (like `browser`, `soak`): the real PyInstaller builds take
  minutes and a GB of disk. The default suite still checks everything that can be checked
  without a build.
- **A frozen copy of release 0.1.0's data** (`tests/fixtures/upgrade/release-0.1.0.zip`, 8 KB,
  `.sqlite` inside because `.gitignore` ignores `*.db`). It must never be regenerated: it stands
  for data real users will have, so every future migration is tried on it. The generator refuses
  to overwrite it or to run once the schema is past v1.
- **The upgrade tests number their hypothetical migration from the real latest** (`latest + 1`),
  so they stay valid when M5-M7 add real migrations.
- **Windows version resources** are per program (`File description` is what Task Manager and
  Task Scheduler show), and validated in a test against the signatures read from PyInstaller's own
  source (its parser needs `pefile`, which installs only on Windows).

## What building it found

These are the reasons to run `smoke.py` rather than trust the unit tests.

1. **`aioftp` crashed the first frozen engine at import** (missing `.dist-info`). Fixed with
   `copy_metadata`; the smoke test's `--version` step catches it (mutation-tested).
2. **My own smoke check was environment-sensitive:** with `COLUMNS` set, `ps` truncated the line
   before `QtWebEngineProcess`, so a healthy UI bundle failed. Now `ps -eww`, with a regression
   test.
3. **A test accidentally ran a real PyInstaller build** because `build.py` loads its own copy of
   `bundle.py` and my monkeypatch hit the other copy. The build-script tests now fail any attempt
   to run a subprocess.

## Merge notes for M5, M6 and M7

A throwaway merge of this branch into the M5 commit (`ff4234b`) had **no conflicts** (not in
`pyproject.toml`, `uv.lock` or `browser.py`, the files both touch). On that merge:

- The two installer-contract tests that **skip** on this branch (task name, `service install
  --exe` / `service uninstall` flags) **run and pass** against M5's real code.
- `test_nothing_in_the_package_spawns_python_dash_m` **fails on exactly the two M5 sites** that
  start the engine from `sys.executable`: `cli/service.py` (the task's command when `--exe` is not
  given) and `engine/main.py` (`respawn`, exit code 3). In a bundle the first would register
  `pagewatch-cli.exe -m pagewatch.engine.main` and the second would restart the wrong program.
- The fix is `packaging/m5-frozen-seams.patch` (3 files, +4 tests). It applies cleanly to a fresh
  M5 + M8 merge. With it, ruff and `mypy --strict` are clean and the merged suite gives 1022
  passed, 1 skipped (a pre-existing skip) and 1 failure: `tests/ui/test_scale.py`'s 200 ms
  scroll/sort/filter test, which ran while a PyInstaller build and a zip were using the CPU and
  passed 3 times out of 3 when re-run alone. A real PyInstaller build of that merged tree passed
  `smoke.py`, and its frozen `pagewatch-cli service install --print-xml` names the packaged
  engine as the task's command. After merging M5 and M8: `git apply
  packaging/m5-frozen-seams.patch`, then delete the patch. If M5 changes those two spots first,
  apply the same two edits by hand.
- **M6:** the build collects `keyring` and `win32ctypes` submodules when installed and copies
  their metadata, but the Windows credential backend and the Check-Macro recorder's browser use
  could not be exercised here. After merging, run `pytest -m packaging`, and on Windows check that
  a stored password survives a restart of the packaged engine.
- **M7:** plugin example files and report templates are collected by the tree walk; plugins load
  from the data folder, so the stdlib is bundled. After merging, run `pytest -m packaging`.
- Nothing here changes the API, so `docs/openapi.json` is untouched.

## Not run here (Linux sandbox: no Windows, no Inno Setup, no PowerShell)

Written and statically tested, **never executed**:

- the Windows `.exe` build, its version resources and icon (the spec's Windows branch is run with
  stand-ins, and the version text is checked against PyInstaller's signatures);
- `ISCC` and `installer.iss` (tests read the script; they cannot compile it). The first compile
  may report a syntax error I could not see;
- `build.ps1`, `verify-install.ps1`, `Start PageWatch.cmd` (no PowerShell or cmd here);
- Task Scheduler registration, `schtasks`, `taskkill`, `ExecAsOriginalUser`, the "all users"
  (UAC) path, silent install/uninstall, the downgrade refusal in Setup, the data-deletion prompt;
- QtWebEngine, Qt plugins and the windowed UI on Windows; the Edge channel; `windows-toasts`,
  `winrt` and `pystray` imports in a Windows bundle (collected when installed, so a Linux build
  cannot show whether they are complete); `AttachConsole`;
- the fallback Chromium download from a bundle (only the driver's `--version` was run);
- SmartScreen and Defender behaviour; code signing; a real sign-out and sign-in;
- the acceptance criterion itself: a clean Windows VM.

What **was** run for real, on Linux, from a real PyInstaller build of this same spec:
the three programs, the engine's spawn worker pool, migrations from bundled resources, the
Playwright driver and a browser check through it, the UI with its QtWebEngine helper process, a
clean stop, a restart with data intact, and the portable zip unpacked and run.

## Open questions for the owner

1. **Graceful stop for the installer.** A `POST /shutdown` (and `pagewatch-cli stop`) would let
   Setup ask the engine to finish in-flight checks instead of killing it. It is an API-shape
   change, which the working agreement says to ask about, so it was not added.
2. **Code signing.** Is there a certificate? Until then SmartScreen warns on every download.
3. **All-users installs.** Per-user is the default and the right fit for a per-user engine; the
   dialog still offers "for all users". Is that wanted, or should it be removed?
4. **Updates stay manual** (SPEC); this does not add an update check.

## Measurements (this sandbox)

- Headless build (engine + CLI): about 70 s, 0.3 GB on disk (336 MB by file size).
- Full build with the UI: about 2 min 15 s, 0.87 GB on disk (PySide6 alone 0.5 GB; Playwright's
  driver 0.13 GB). The portable zip of it is 517 MB. Windows sizes will differ (no shared-library
  symlinks, a different Qt build); measure them on the first Windows build.
- Smoke test: 12 s headless, 23 s with the browser and UI stages.
- Tests: default suite 905 passed, 3 skipped (from 791 + 1); `-m browser` 12 passed; `-m packaging`
  3 passed in about 4 minutes.

## Text for `docs/ARCHITECTURE.md` after the merge

> **Packaging** (`packaging/`). One PyInstaller one-folder build holds three programs
> (`pagewatch-engine`, `-ui`, `-cli`) over a shared `_internal/`. Every program starts through
> `pagewatch.engine.frozen.launch()` (`freeze_support()` first, then the component's `main()`), and
> starts its siblings through `component_command()`, never `sys.executable -m`. `bundle.py` says
> what goes in; `build.py` builds, `smoke.py` runs the result, `installer.iss` wraps it (per-user,
> `service install --exe`, stop-engine/upgrade/restart, data kept). Upgrades are install-over; the
> engine migrates its data at start-up after `pre-migration-v<N>` backup, rolls back a failed
> migration and refuses newer data. Portable mode is `--data-dir`.
