# TASKS (M8): Packaging

Branch `claude/m8-packaging`, from 90ca022 (M0-M4, 791 tests). M5, M6 and M7 are built in parallel
in other sessions, so this checklist and `docs/DECISIONS_M8.md` live beside `TASKS.md` and
`docs/DECISIONS.md` instead of editing them (the owner merges them afterwards).

SPEC milestone M8: *PyInstaller build, Inno Setup installer, upgrade path with pre-migration
backup, short user guide.* Acceptance: *a clean Windows VM installs, autostarts at logon and
survives an upgrade with data intact.* Appendix row owned by M8: *portable install, multiple
instances, via `--data-dir`.*

Rules from the SPEC working agreement: deviations are logged (here: `docs/DECISIONS_M8.md`), ruff +
`mypy --strict` + the full suite green, one commit. New dependency: `pyinstaller` only (named in
the SPEC's technology table) in its own `packaging` dependency group, so it never reaches the
runtime install. Inno Setup is a Windows tool, not a Python dependency.

## Audit: SPEC vs repository at 90ca022

| SPEC requirement | State at 90ca022 | Task |
| --- | --- | --- |
| `packaging/pagewatch.spec`, `installer.iss` (layout section) | Missing | S2, S4 |
| Three executables from one package: `pagewatch-engine`, `-ui`, `-cli` | Console scripts only (`pyproject.toml`) | S2.2 |
| No system Python required | **Three places start a child with `sys.executable -m ...`**: `ui/main.py` (start engine), `engine/tray.py` (open UI), `engine/fetch/browser.py` (`playwright install chromium`). All break in a frozen build | S1 |
| Worker pool uses the `spawn` start method | Needs `multiprocessing.freeze_support()` first thing in every frozen entry point, or each worker re-runs the whole app | S1.3 |
| Migrations, `data/*.yaml/.txt`, `ui/web/diff.css` read through `importlib.resources` | Not collected by PyInstaller by default | S2.1 |
| Playwright drives Edge, so its Node driver must ship (no hook exists) | Missing | S2.1 |
| Windows without a console for the engine (starts at logon) | `print(..., file=sys.stderr)` and logging to a console assume streams exist | S1.4 |
| Autostart: Task Scheduler task at logon | M5 builds `pagewatch-cli service install --exe <engine>`; M8 installs/removes it | S4.3 |
| Upgrade: "install the new build over the old one. Migrations run at startup after an automatic backup" | `migrate()` backs up (`pre-migration-v{n}-{stamp}.db`) but no test proves upgrade, rollback or downgrade refusal | S5 |
| Installer must stop a running engine, keep user data on uninstall | Missing | S4.4 |
| Portable install, multiple instances via `--data-dir` | Flag exists on all three entry points; side-by-side behaviour untested; no portable artifact | S3, S5.4 |
| Short user guide | Missing | S6 |
| Windows VM acceptance | Cannot run in this sandbox | S7 (script + checklist) |

## S0 Setup

- [x] S0.1 Branch `claude/m8-packaging` from 90ca022.
- [x] S0.2 Baseline green at 90ca022 (full suite, ruff, `mypy --strict`); Qt system libraries installed in this sandbox.
- [x] S0.3 `pyinstaller` (6.22.3) in the `dev` dependency group (not a separate group: the packaging tests must always run).

## S1 Frozen-aware startup (`src/pagewatch/engine/frozen.py`)

- [x] S1.1 `frozen.py`: `is_frozen`, `app_dir`, `component_command(engine|ui|cli, *args)`, `playwright_install_command()`.
- [x] S1.2 Route the three `sys.executable -m` sites through it (one-line changes, away from the hunks M5 edits).
- [x] S1.3 `launch(component)`: `freeze_support()` first, then the component's `main`.
- [x] S1.4 `prepare_streams()`: windowed Windows builds have `sys.stdout/stderr = None`; give them a null sink, and attach to a parent console when there is one.
- [x] S1.5 Unit tests: command lines frozen and not, Windows `.exe` suffix, stream repair, `freeze_support` ordering, entry scripts delegate.
- [x] S1.6 Audit test: no bare `sys.executable` spawn outside `frozen.py` (fails loudly on the two M5 sites after the merge, with the fix in `DECISIONS_M8.md`).

## S2 PyInstaller build

- [x] S2.1 `packaging/bundle.py` (plain Python, no PyInstaller import): package data files, hidden imports, excludes, component table.
- [x] S2.2 `packaging/pagewatch.spec`: three EXEs (engine and UI windowed, CLI console), one shared one-folder `PageWatch/`, icon, Windows version resource, no UPX.
- [x] S2.3 `packaging/entry/{engine,ui,cli}.py` (3-line launchers).
- [x] S2.4 `packaging/build.py`: version, version resource, PyInstaller, optional Inno compile, portable zip, SHA-256 file.
- [x] S2.5 `packaging/assets/pagewatch.ico` from `tools/make_icon.py` (Pillow; same drawing as the tray).
- [x] S2.6 Real Linux build of the same spec, run the frozen executables (S3).

## S3 Frozen smoke test (`packaging/smoke.py`, stdlib only)

- [x] S3.1 Run the built `pagewatch-engine` on a temp data folder: `/health` with the token, token required, lockfile.
- [x] S3.2 Real check through the frozen process pool: add a bookmark on a local page, change it, exactly one change recorded (proves spawn workers, lxml, zstd, sqlite, migrations from bundled resources).
- [x] S3.3 `pagewatch-cli` against it; graceful stop (exit 0, lockfile gone).
- [x] S3.4 Layout checks: Playwright driver files, migrations, data files, tzdata.
- [x] S3.5 Optional stages: browser fetch through the bundled Playwright driver; UI starts offscreen.
- [x] S3.6 `@pytest.mark.packaging` test runs the build + smoke (opt in: `-m packaging`).

## S4 Installer (`packaging/installer.iss`)

- [x] S4.1 Per-user install by default (`PrivilegesRequired=lowest`, admin selectable), pinned `AppId`, version from `/DAppVersion`.
- [x] S4.2 Start menu, optional desktop icon, user guide link.
- [x] S4.3 "Start PageWatch when I sign in" task (default on): `pagewatch-cli service install --exe <engine>`; start the engine after install; uninstall removes the task.
- [x] S4.4 Upgrade: stop the engine before copying, restart it after; refuse to downgrade a newer install; uninstall keeps `%LOCALAPPDATA%\PageWatch` unless the user opts in.
- [x] S4.5 Static tests on the script (cannot be compiled here).

## S5 Upgrade path and portable mode (tests)

- [x] S5.1 Upgrade with data: v1 data folder populated through the real engine, then a v2 migration; bookmarks, versions, blobs intact; backup valid and holding the pre-upgrade rows.
- [x] S5.2 A failing migration leaves the database at v1 (rolled back) and the backup in place.
- [x] S5.3 Downgrade refused (`SchemaTooNew`), data untouched.
- [x] S5.4 Portable: two engines side by side on different folders; `--data-dir` accepted by engine, UI, CLI; relative paths; nothing written outside the folder.

## S6 Documentation and assets

- [x] S6.1 `docs/USER_GUIDE.md` (short; shipped in the installer).
- [x] S6.2 `packaging/README.md`: build, verify, release steps; portable launcher.
- [x] S6.3 `packaging/portable/` launcher and README.
- [x] S6.4 `docs/DECISIONS_M8.md`: deviations, merge notes for M5/M6/M7, what could not be run here.

## S7 Windows VM acceptance (manual; script provided, not runnable here)

- [x] S7.1 `packaging/verify-install.ps1` written: silent install, task registered, engine running, data added, upgrade over it, data intact, downgrade refused, uninstall keeps data. **Not run** (no Windows/PowerShell here).
- [ ] S7.1b Run it on a clean Windows VM (owner).
- [x] S7.2 Manual checklist written in `packaging/README.md`: sign out/in autostart, Edge and Chromium fallback, toast, tray, Defender/SmartScreen.
- [ ] S7.2b Work through it on that VM (owner).

## S8 Finish

- [x] S8.1 ruff, `mypy --strict`, full suite: all exit code 0. Default suite **905 passed, 3 skipped** (was 791 + 1); `-m browser` 12 passed; `-m packaging` 3 passed (4 min). The skips: two installer-contract tests that come alive after M5 merges, one pre-existing (non-root POSIX user).
- [x] S8.2 Commit, push `claude/m8-packaging`, stop.

## Added along the way (not in the first plan)

- [x] `packaging/m5-frozen-seams.patch`: a throwaway merge of this branch into the M5 commit
  (no conflicts) showed two M5 spots that start the engine from `sys.executable`; the patch fixes
  both, applies cleanly, and with it ruff, `mypy --strict` and the merged suite pass (1022 passed;
  one UI timing test failed under CPU load from a concurrent build and passed 3/3 alone).
  A real build of the merged tree passed the smoke test, and its frozen CLI registers a task that
  runs the packaged engine.
- [x] `tests/fixtures/upgrade/release-0.1.0.zip` + `tools/gen_upgrade_fixture.py`: a frozen data
  folder from the first release, so every later migration is tried on real released data.
- [x] Playwright's bundled driver is *executed* by the smoke test (`--version`), not just found.
- [x] Mutation checks of the tests that matter (each failed as it should, then was restored): no
  backup, no transaction, missing metadata / migrations / driver, non-executable driver, a bare
  `sys.executable` spawn, no stream repair.

## M8 acceptance criterion: where it is proved

| Part of "a clean Windows VM installs, autostarts at logon and survives an upgrade with data intact" | Proof here | Still to do on Windows |
| --- | --- | --- |
| The programs run without Python | `smoke.py` on a real PyInstaller build (engine, worker pool, CLI, browser, UI, stop, restart); `pytest -m packaging` | Same on Windows (`build.ps1 -Smoke`) |
| Installs | `installer.iss` (static tests only) | `verify-install.ps1`; compile with ISCC |
| Autostarts at logon | M5's `service install --exe` called by the installer; the task XML generator is tested (M5) | `verify-install.ps1` checks the task and has Task Scheduler start the engine; a real sign-out/in by hand |
| Survives an upgrade with data intact | `test_upgrade.py`: rows and blobs intact, backup equals the old state, failed migration rolls back, newer data refused, restarts add no backups, frozen release-0.1.0 data upgrades | `verify-install.ps1 -OldInstaller ... -NewInstaller ...` |
| Portable install, multiple instances | `test_portable.py` (two real engines, nothing written elsewhere); portable zip unpacked and smoke-tested | `Start PageWatch.cmd` on Windows |

## Cannot be done in this sandbox

Linux, no Windows, no Inno Setup compiler, no PowerShell: the `.exe` build, `ISCC`, Task Scheduler,
Authenticode signing and the VM acceptance run are written and statically tested but **not executed**.
`docs/DECISIONS_M8.md` lists each one.
