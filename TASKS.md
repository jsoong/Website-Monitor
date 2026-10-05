# TASKS: Unattended operation (milestone M5)

Scope: SPEC.md milestone M5 — *resume detection, connectivity probe, catch-up, battery policy,
retries, error notifications, durable action queue, retention and GC, automatic backup, RSS guard,
`service install`* — with the sections that define them: *Scheduler → Sleep, resume and
connectivity*, *Alert gating → Errors*, *Actions → Execution rules*, *Data model → Retention*,
*Import, export, reports and backup → Automatic backup / Manual backup and restore*, *Continuous
operation and reliability*, and the M5 acceptance criteria. Also the two items earlier milestones
parked here: heartbeat-lateness resume detection (DECISIONS M0) and the per-job worker timeout
(DECISIONS M2, M4 "Known limits").

Out of scope (later milestones, not touched): email/ntfy/export/run_program/scrape/script actions,
the Problems screen and logins/macros/proxies (M6); the Settings screen (its retention and backup
panels need nothing the API does not already give: `PUT /settings`); Follow-Links, import/export,
reports, plugins (M7); packaging and the real Windows install check (M8). The spec's M6 criterion
"a failing SMTP server leads to retries, then a Problems entry" stays M6; M5 only makes sure the
queue underneath it is durable and ordered.

Rules from SPEC "Working agreement": one milestone, deviations logged in `docs/DECISIONS.md`,
ARCHITECTURE.md updated, full suite green, one commit. **No new dependency** (psutil, httpx and
zipfile already cover everything). **No schema migration**: last-run times use the `setting`
table under a `_state.` prefix that `SettingsStore` never loads as a setting.

The M4 checklist this file replaces is in git at `90ca022`.

## Audit: SPEC vs repository at 90ca022

Baseline: 791 passed, 1 skipped (default suite) after installing the Qt system libraries.
Note: in this session's checkout the local branch pointed at `e259c5e` (only the SPEC upload) while
the M4 commit `90ca022` existed only as a dangling child of it (the remote branch already had it; the
local remote-tracking ref was stale). The local branch was fast-forwarded to `90ca022` first
(lossless: `e259c5e` is its parent).

| SPEC requirement | State in the repo | Task |
| --- | --- | --- |
| Resume detection: wall-vs-monotonic drift > 30 s | Missing; `FakeClock.jump_wall` exists, nothing reads it | S3.2 |
| Resume detection: `WM_POWERBROADCAST` on a hidden window; `WM_QUERYENDSESSION` shutdown | Missing | S3.1 |
| Heartbeat lateness (monotonic may include suspend on Windows) | Missing; promised in DECISIONS M0 | S3.2 |
| After resume: probe a configurable URL for up to 2 min before dispatching | Missing | S3.3, S3.5 |
| Offline mode: pause dispatch, stop counting errors, resume when the probe succeeds | `Scheduler.online`, `Engine.online`, tray OFFLINE state exist but nothing ever sets them; `_fail` already skips counting when offline | S3.3, S4.1 |
| Catch up once: one `catchup` check per overdue bookmark, spread over min(5 min, count × 1 s) | Missing; `Trigger.CATCHUP` exists, unused; overdue bookmarks dispatch all at once as `schedule` | S2.2, S3.5 |
| `days` / `window` limits apply to every mode | Applied when a due time is computed, not to a catch-up after a sleep | S2.2 |
| Battery: read once a minute; `on_battery` normal / slow / pause | `slow` (×4) exists in schedule math, `Engine.on_battery` is never set, `pause` is honoured nowhere | S2.3, S3.4 |
| Global "pause on battery saver" (off by default); "keep awake while AutoWatch runs" (off) | Missing | S3.4 |
| Transient errors retry once after 60 s; after N the bookmark turns `error` and notifies once | Done in M1 (`runner._fail`) | reuse |
| Error counter resets on success **or when the user opens the bookmark** | Success only | S4.2 |
| Failures while offline are not counted at all | Only if `e.online` were ever false | S4.1 |
| Per-job timeout that kills and rebuilds the pool (catastrophic regex / hung PDF) | Missing; deferred from M2 and M4 | S4.3 |
| Durable action queue: row per (change, index), idempotent, 1/5/30 min back-off, 5 attempts | Done in M1; **but jobs of one change run concurrently, so "in configured order" and "`mark_read` always last" are not guaranteed**; the loop sleeps a flat 60 s instead of until the next retry | S5.1, S5.2 |
| Retention: keep pointers, pinned, last 20 changed versions; global disk cap 10 GB prunes oldest first; change rows go with their versions | Missing (`keep_changed_versions`, `disk_cap_gb` settings exist, unused) | S6.2, S6.4 |
| Nightly: delete unreferenced versions/blobs, `check_run` older than 30 days | Missing; `BlobStore.iter_blobs` / `iter_stale_temp_files` exist, unused | S6.3, S6.5 |
| Blob dedupe vs GC safety | `BlobStore._write` skips an existing blob without touching it: a GC could delete a blob a worker just decided to reuse | S6.1 |
| Automatic backup: daily 03:00 local or next wake; online backup + settings, macros, plugins in a zip; keep 14; blobs optional | Only the pre-migration `.db` copy exists | S7.1, S7.2, S8.1 |
| Manual backup and restore (Settings or CLI); engine restarts itself after a restore | Missing (`POST /backup`, `/restore`, CLI `backup`, `restore`) | S7.3–S7.5 |
| Engine RSS 1.5 GB → recycle browser, then exit code 3 | Missing; `/health.rss_mb` is the engine process only | S8.3 |
| Event-loop lag 10 s → log a stack dump | Missing | S8.4 |
| Hourly RSS, CPU, queue-length samples in `metric`, feeding `/health` | Missing (table and index exist) | S8.2 |
| Startup: close open `check_run` as `error: interrupted`, re-queue its bookmark | Done in M1 (`repo.close_interrupted_runs`) — **never exercised by a real kill -9** | S10.3 |
| WAL checkpoint on graceful shutdown; 10 s grace for in-flight checks | Done (`Database` writer, `Scheduler.stop`) | reuse |
| `pagewatch-cli service install\|uninstall` (Task Scheduler: logon trigger, restart every 1 min, no run-time limit, allowed on battery, never a second instance) | Missing | S9.1, S9.2 |
| Exit code 3 "restart me" | Defined (`EXIT_RESTART`), nothing returns it | S7.5, S8.3 |
| `engine_state` event, tray OFFLINE/PAUSED states | Event type and tray logic exist; nothing publishes online/offline | S3.5 |
| Soak: 24 h, 1,000 bookmarks, resource budgets; capacity run at 10,000 | `soak` marker declared, no tests | S10.4 |

## S0 Setup

- [x] S0.1 Fast-forward `claude/focused-mccarthy-2a74te` to the M4 commit `90ca022`.
- [x] S0.2 Qt system libraries installed in this sandbox; baseline 791 passed + 1 skipped.

## S1 Models and settings (`models.py`)

- [x] S1.1 `Settings`: connectivity (`connectivity_url`, `connectivity_timeout_s`, `probe_interval_s`, `resume_probe_window_s`, `offline_probe_s`), resume (`resume_drift_s`, `os_power_events`), `catchup_spread_s`, battery (`battery_poll_s`, `pause_on_battery_saver`, `keep_awake`), backup (`backup_enabled`, `backup_time`, `backup_keep`, `backup_include_blobs`), retention (`maintenance_time`, `check_run_retention_days`, `metric_retention_days`, `blob_grace_h` with a 1 h minimum), guard (`rss_limit_mb`, `rss_check_s`, `rss_grace_s`, `loop_lag_limit_s`, `metric_interval_s`), `worker_job_timeout_s`.
- [x] S1.2 `ActionsConfig`: `mark_read` is always ordered last (stable otherwise), so `action_index` matches execution order.
- [x] S1.3 `HealthOut` (additive): `rss_total_mb`, `cpu_percent`, `last_backup_at`, `last_maintenance_at`. Bodies `BackupRequest/BackupOut`, `RestoreRequest/RestoreOut`.
- [x] S1.4 `docs/openapi.json` regenerated.

## S2 Scheduler policy (`scheduler.py`)

- [x] S2.1 Named dispatch holds beside paused/offline; manual checks still run.
- [x] S2.2 `catch_up()` (hotsites first, staggered over `min(spread, count x 1 s)`, already-waiting items re-timed) and `Engine.catch_up` (a bookmark whose `days`/`window` forbid "now" moves to its next allowed start).
- [x] S2.3 Per-bookmark `on_battery: pause` parking; policy kept in sync on create, patch, folder-default change and start-up; AC power returning catches up.
- [x] S2.4 `reschedule(id, due)`, `set_battery_pause`, `overdue_ids`.

## S3 Power, connectivity, battery (`engine/power.py`)

- [x] S3.1 `PowerBackend`, `NullPowerBackend`, Windows `SystemPowerBackend` (hidden window, `GetSystemPowerStatus`, `SetThreadExecutionState`). **The Windows-only code is unexecuted** (manual check); it has an off-switch (`os_power_events`).
- [x] S3.2 `PowerMonitor`: resume by wall-vs-monotonic drift, by heartbeat lateness, or by OS message; one resume per wake.
- [x] S3.3 `Connectivity`: probe, single-flight and short cache, `wait_for_network`, offline mode with a recovery loop.
- [x] S3.4 Battery poll, battery-saver pause, keep-awake.
- [x] S3.5 Engine wiring: resume sequence (hold, wait, plan the catch-up, **then** release), start-up probe and catch-up, `engine_state` events (the tray already refreshes on them), action queue kicked on resume.

## S4 Errors, retries, worker timeout

- [x] S4.1 Runner verifies connectivity before counting a network-looking failure; offline failures are `skipped` / `offline:<kind>`, not counted, the bookmark stays due.
- [x] S4.2 Mark-read clears `consecutive_errors` and the `error` status.
- [x] S4.3 `WorkerPool` time limit: kill the workers, rebuild the pool, `WorkerTimeout`; the runner reports a `parse` error.

## S5 Durable action queue (`actions/queue.py`)

- [x] S5.1 A change's jobs run one at a time in order; a `mark_read` is skipped if an earlier action finally failed.
- [x] S5.2 The loop sleeps until the next retry (at most 60 s).
- [x] S5.3 Tests: order, `mark_read` last, back-off 1/5/30 minutes exact, failure after 5 attempts reported once, restart durability, crash re-runs once, nothing sent twice, deleted bookmark's jobs vanish.

## S6 Retention and GC (`store/retention.py`)

- [x] S6.1 `BlobStore` refreshes a blob's mtime when it reuses it (mtime = last use).
- [x] S6.2 Version pruning (pointers, pinned, newest N), changes and jobs go with their versions, re-checked inside the delete.
- [x] S6.3 Blob mark-and-sweep: nested references, grace period, stale temp files; a failing scan sweeps nothing.
- [x] S6.4 Disk cap, with an explicit "unreachable" report.
- [x] S6.5 `check_run` and `metric` purge, WAL checkpoint, chunked writes, scans on short-lived connections.

## S7 Backup and restore (`store/backup.py`)

- [x] S7.1 `create_backup` (manifest, online snapshot, settings, macros, plugins, optional blobs; atomic).
- [x] S7.2 Keep the newest 14 automatic backups only.
- [x] S7.3 Validate, stage, apply at the next start-up after copying the current database aside; a bad staged zip is set aside.
- [x] S7.4 `POST /backup`, `POST /restore`; CLI `backup [--blobs] [--out]`, `restore <zip>`.
- [x] S7.5 Exit code 3 after a restore; self-respawn unless `--supervised`, with a one-minute loop guard.

## S8 Maintenance loop and resource guard (`maintenance.py`, `guard.py`)

- [x] S8.1 Backup at 03:00 and retention at 03:30 "or at the next wake"; state persisted; a failure retried after an hour.
- [x] S8.2 Hourly samples into `metric`; `/health` reports them.
- [x] S8.3 RSS guard (process tree): recycle the browser, then exit code 3.
- [x] S8.4 Event-loop watchdog thread with a stack dump.

## S9 `service install` (`cli/service.py`)

- [x] S9.1 Task Scheduler XML (logon trigger, restart every minute, no run-time limit, on battery, one instance).
- [x] S9.2 `service install|uninstall|status` through `schtasks` (Windows); `--print-xml` works anywhere; no engine needed. **`schtasks` itself is a manual check.**
- [x] S9.3 `pagewatch-engine --supervised`.

## S10 Tests

- [x] S10.1 Unit: workers (real process), retention, backup/restore, service XML, guard and watchdog (real stalled loop), maintenance, the reader-pool regression.
- [x] S10.2 Whole engine under `FakeClock`: 3-hour sleep, 10 minutes offline, resume waiting for the network, an OS message plus drift as one resume, window respected on catch-up, restart after hours, battery pause/slow/saver, keep-awake, restore through the API and CLI, catastrophic regex with a real worker process.
- [x] S10.3 **kill -9 mid-check** with real engine processes; restore and `--supervised` with real processes.
- [x] S10.4 Soak (`-m soak`): see the results below.

## S11 Finish

- [x] S11.1 `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (M5 section), `docs/openapi.json`.
- [x] S11.2 ruff (lint and format), mypy `--strict`, full default suite green.
- [ ] S11.3 Commit, push to `claude/focused-mccarthy-2a74te`, stop (no M6).

## Added along the way (not in the first plan)

- [x] CLI `status` shows offline / on battery and the last backup.
- [x] `Settings.os_power_events`: an off-switch for the unexecuted native Windows code.
- [x] `Database.backup_sync` and retention's scans use short-lived connections; SQLite page caches cut from 20 MB x 5 to 2 MB per reader and 4 MB for the writer (both found by the soak, see DECISIONS).
- [x] `BrowserManager.recycle()` (the memory guard's first remedy).
- [x] A review of the Windows ctypes code found and fixed two defects before it ever ran (a `ctypes.wintypes` class that does not exist; missing argument/result types).

## M5 acceptance criteria -> where they are proved

| Criterion (SPEC M5) | Proof |
| --- | --- |
| A simulated 3-hour sleep yields one staggered catch-up check per overdue bookmark | `test_m5_unattended.py::test_a_three_hour_sleep_...` (12 bookmarks, exactly one `catchup` check each, 12 distinct start times spread over 11 s, then back to the normal rhythm). Mutation-tested: planning the catch-up after releasing the hold fails it. |
| 10 minutes offline adds zero to any error counter | `test_ten_minutes_offline_adds_zero_to_any_error_counter` (5 bookmarks: counters 0, status `ok`, one `skipped offline:connection` run each, no toast; then recovery and catch-up). Mutation-tested. |
| kill -9 mid-check, then restart: no corrupt rows, interrupted check re-queued | `test_m5_process.py::test_kill_9_mid_check_...` (real engine process, real SIGKILL: `integrity_check` ok, no foreign-key violations, nothing half-written; after restart the run is closed `error: interrupted`, counter untouched, bookmark re-checked as `catchup`) |
| 24-hour soak stays within the resource budgets | `tests/soak/test_soak.py` (opt-in). **Simulated 24 h** at 1,000 bookmarks: 62k checks, no backlog, nobody late, 0 errors, no orphan blob, RSS +4.0%. **Real engine process**, 1,000 bookmarks: idle CPU 0.23% of one core; RSS 80 MB engine process / 302 MB with workers. **Not a real 24-hour run.** |

Open question for the owner: the spec's "<= 250 MB RSS at 1,000 bookmarks" is met by the engine
process (80 MB) and not by the process tree (302 MB). See DECISIONS (M5, Soak).

## Manual checks that cannot run in this sandbox

- Windows: the hidden-window power messages, `GetSystemPowerStatus` (battery, battery saver), `SetThreadExecutionState`, `WM_QUERYENDSESSION`, real suspend/resume of a laptop. **The ctypes code is unexecuted.**
- `schtasks /Create` with the generated XML on a Windows machine, and the task restarting the engine after exit code 3 (only the XML is checked here).
- A real 24-hour soak, 14 days of uptime, and the 2-hour 10,000-bookmark capacity run.
- The default connectivity URL on the owner's network.
