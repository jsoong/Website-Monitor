# TASKS

One checklist per milestone, in the order the milestones were merged. Task ids (`S0`, `S1`, ...) are
local to their milestone. The M4 checklist is in git at `90ca022`; earlier ones are in the history
of this file.

| Part | Milestone | State |
| --- | --- | --- |
| M5 | Unattended operation, and its Settings and Problems screens | Done |
| M6 | Actions (email, ntfy, webhook, export, run program, scrape, script) | Done |
| M6 | Logins and proxies (macros, sign-in, cookies, proxies, per-host, portal experiment tooling) | Done; **the portal experiment itself has not been run** |
| M7 | Power features | Done |
| M8 | Packaging | Done here; the Windows VM acceptance run is the owner's |
| Merge | M6, M7 and M8 onto the M5 line, and the seams between them | Done |
| Merge | M6 logins onto that line: the last piece of v1 | See the last part |

---

# M5 · Unattended operation

Scope: SPEC.md milestone M5 — *resume detection, connectivity probe, catch-up, battery policy,
retries, error notifications, durable action queue, retention and GC, automatic backup, RSS guard,
`service install`* — with the sections that define them: *Scheduler → Sleep, resume and
connectivity*, *Alert gating → Errors*, *Actions → Execution rules*, *Data model → Retention*,
*Import, export, reports and backup → Automatic backup / Manual backup and restore*, *Continuous
operation and reliability*, and the M5 acceptance criteria. Also the two items earlier milestones
parked here: heartbeat-lateness resume detection (DECISIONS M0) and the per-job worker timeout
(DECISIONS M2, M4 "Known limits").

Out of scope (later milestones, not touched): email/ntfy/export/run_program/scrape/script actions
and logins/macros/proxies (M6); Follow-Links, import/export, reports, plugins (M7); packaging and the
real Windows install check (M8). The spec's M6 criterion "a failing SMTP server leads to retries, then
a Problems entry" stays M6; M5 only makes sure the queue underneath it is durable and ordered.

**The Settings and Problems screens are in scope for M5** (owner's instruction for the session that
wrote "Part 2" below; DECISIONS M3 had deferred them as "M5/M6" and the first M5 pass left them out).
The engine half of M5 was committed first (`ff4234b`, `a051864`); Part 2 adds only the two screens, the
two small read endpoints they need, and the UI reconnect that makes "Restore" usable from Settings.

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
- [x] S11.3 Commit and push the engine half (`ff4234b`, `a051864` on `claude/focused-mccarthy-2a74te`).

# M5 Part 2: the Settings and Problems screens

Starting point: HEAD `a051864` (the session brief assumed `90ca022`; the engine half was already
there). Baseline in this container, after installing the Qt system libraries: **905 passed, 1 skipped** (not the 791 in the brief, which predates the engine half).

Spec text this implements: *Desktop UI → Other screens* ("Settings (defaults, network, email,
notifications, retention, backup, data folder), Problems (errors and failed actions)"), *Actions →
Execution rules* ("Failed jobs appear in the UI's Problems list with the last error"), *Scheduler →
Capacity* ("Settings shows current demand per pool and per host ... browser demand against capacity ...
warns above 80%"), *Observability* ("The DEBUG level can be toggled in Settings without a restart"),
*Backup* ("Manual backup and restore: from Settings or the CLI; the engine restarts itself").

Not done on purpose (see DECISIONS, M5 part 2): the Email panel is a placeholder (SMTP settings and the
email action are M6); the scheduler stretching an over-limit host's intervals and the editor's per-host
warning (Scheduler work with no milestone of its own; Settings only *shows* the over-limit hosts);
retry/dismiss buttons for failed actions (an API shape the owner has not asked for).

## S12 Engine: two read endpoints and one settings fix

- [x] S12.0 Baseline green in this container (Qt libraries installed, default suite).
- [x] S12.1 `models.py`: `ProblemOut` / `ProblemsOut`, `PoolDemand` / `HostDemand` / `CapacityOut` (additive).
- [x] S12.2 `engine/problems.py` + `GET /problems`: bookmarks in `error` / `needs_login` with the last failing
  check's reason, and `failed` action jobs with their last error; newest first, `total`, `limit`.
- [x] S12.3 `engine/capacity.py` + `GET /capacity`: demand = sum of 1/interval per pool (static, browser) and
  per host; capacity from the pool size and the rolling mean check time; per-host ceiling from the spacing;
  `warn` above 80%.
- [x] S12.4 `SettingsStore.update`: `null` removes a `host_overrides` entry (merge could never remove one).
- [x] S12.5 Tests: problems (error with reason, needs-login, failed action, ordering, cleared by mark-read /
  success), capacity (known inputs, browser warn, host over ceiling, 10,000-bookmark timing), host-override
  removal, auth required.
- [x] S12.6 `docs/openapi.json` regenerated.

## S13 UI: client and reconnect

- [x] S13.1 `ApiClient`: `problems()`, `capacity()`, `put_settings()`, `backup()`, `restore()`, `retarget()`.
- [x] S13.2 `EventStream.retarget()`; `MainWindow` re-reads the lockfile while disconnected and retargets
  when the engine came back on a new port/token (restore, memory-guard exit 3, manual restart).
- [x] S13.3 Tests: retarget against a second engine on the same data folder.

## S14 UI: Problems window (`ui/windows/problems.py`)

- [x] S14.1 Table: kind, bookmark, last error, when, attempts; newest first; refresh on `problem`,
  `bookmark_updated` and `engine_state` events (debounced) and on demand.
- [x] S14.2 Actions: Check now (bookmark errors), Show bookmark (selects it in the main list), Open URL, Copy error.
- [x] S14.3 Main window: Problems toolbar action with a count in the status bar; Ctrl+Shift+P.
- [x] S14.4 Tests against a real engine: an error bookmark and a failed action appear, clear after a good
  check / mark-read, Show bookmark selects the row.

## S15 UI: Settings dialog (`ui/windows/settings.py`)

- [x] S15.1 Pages: General (default schedule, user agent, time zone, debug logging), Network (proxy,
  connectivity, politeness, pools, per-host overrides), Notifications, Email (placeholder), Power & resources,
  Retention, Backup, Data folder, Capacity.
- [x] S15.2 Sends only the fields that changed; engine validation errors shown in place; fields that need a
  restart are labelled.
- [x] S15.3 Backup page: back up now (with/without blobs), list the folder's backups, restore (confirmed,
  then the UI reconnects).
- [x] S15.4 Capacity page: pools and hosts with the 80% warning; Data folder page: sizes off the GUI thread.
- [x] S15.5 Main window: Settings action (Ctrl+,).
- [x] S15.6 Tests: untouched dialog sends nothing, a changed field round-trips, bad value shown not sent,
  host override added and removed, debug logging toggled live, backup now writes a zip, restore through
  the dialog restarts and the window reconnects.

## S16 Finish (Part 2)

- [x] S16.1 `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (M5 part 2), `docs/openapi.json`.
- [x] S16.2 `ruff check`, `ruff format --check`, `mypy --strict`, full default suite: all exit 0 (**956 passed, 1 skipped**: 905 + 51 new).
- [x] S16.3 Commit, push to `claude/focused-mccarthy-2a74te`, stop (no M6).

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

## M5 Part 2: what the tests prove

| Requirement | Proof |
| --- | --- |
| Problems lists a failing bookmark with its last error and clears when it recovers or is opened | `test_m5_problems_capacity.py` (real engine, fake clock), `tests/ui/test_problems.py` (live update from the engine's `problem` event) |
| Failed actions are listed after the 5th failure, with attempts, and clear when the change is read | both files above; mutation-tested |
| Demand per pool and per host, browser warning above 80%, host over its limit | `test_m5_problems_capacity.py` (cases at 150%, 90%, 75%, 50% of capacity; 10,000 bookmarks in < 2 s, measured 188 ms) |
| Settings sends only changes, shows the engine's refusal, labels restart-only fields | `tests/ui/test_settings.py` (21 tests); mutation-tested (send-all, rebase, host removal) |
| Back up now, restore from Settings, the window follows the restarted engine | `test_restoring_through_the_window_restarts_into_the_backup_and_the_window_follows` (two real engines on one folder), `tests/ui/test_reconnect.py` |

## Manual checks that cannot run in this sandbox (Part 2)

- How the Settings and Problems windows look on Windows (they were rendered offscreen on Linux): fonts,
  button order, dark mode.
- A restore under Task Scheduler (`--supervised`).


---

# M6 · Actions

Built on branch `claude/m6-actions` from 90ca022 (M0-M4, 791 tests) in parallel with M5, M7 and M8,
and merged into the M5 line (see *Merge* at the end). The scope below is what that session was
asked to build; the logins half of SPEC's M6 is the next part.

## Scope decision (read this first)

SPEC.md names M6 **"Actions and logins"**. The request for this session described M6 as the
actions half: notifications, alert delivery channels, webhooks, executing actions when a change
passes the gate, and the UI wiring for them. This session implements exactly that half.

| SPEC M6 item | This session |
| --- | --- |
| Email, ntfy, export, run program, scrape and script actions | **Done** |
| Webhook action (SPEC: "post-v1"; the request names webhooks explicitly) | **Done**, see DECISIONS.md (M6) |
| Pushover (SPEC: post-v1; not requested) | Not built; stays "not available" |
| Keyring-backed secret store (SMTP password, ntfy token, FTP password) | **Done** (actions need it) |
| Failed jobs in the UI's Problems list; retries then a Problems entry | **Done** (API + dialog) |
| Alert privacy (`private` sends no page content) | **Done** |
| Optional semi-attended sign-in, portal experiment | **Not done** (logins half) |
| Check-Macro recorder and replay | **Not done** (logins half) |
| Cookie import, Basic/Digest auth in the fetchers, per-bookmark proxies | **Not done** (logins half) |
| Per-host overrides in the scheduler | **Not done** (logins half; the `Settings.host_overrides` model already exists) |

The logins half needs the fetcher, runner and scheduler (`needs_login` pauses a bookmark). It is
the natural second M6 session now that M5 and M6 are on one line.

## Audit: SPEC vs repository at 90ca022

| SPEC requirement | State in the repo | Task |
| --- | --- | --- |
| `action_job` durable queue, retries at 1/5/30 min, 5 attempts, `problem` event | Done (`actions/queue.py`) | reuse unchanged |
| `toast`, `sound`, `open`, `mark_read` | Done (`actions/builtin.py`, `toast.py`) | reuse |
| `email`, `export`, `run_program`, `ntfy`, `scrape`, `script`, `webhook` | Registered as `_unavailable(...)` | S3 |
| `ActionConfig.params` | Free `dict`, no validation | S1.2 |
| SMTP / ntfy server settings | Absent | S1.3 |
| `SecretStore` | Interface plus `NullSecrets`/`MemorySecrets`; no keyring | S2.5 |
| Jinja2 sandbox templates; `{var}` path/argument templates | Absent | S2.1 |
| Alert text for a change (added text, changes-only HTML) | Only the 200-char `summary` | S2.2, S2.3 |
| Alert privacy | `AlertContext.body` for toasts only | S3 (all channels) |
| Problems list for failed action jobs | `problem` event only; no way to list or retry | S4.1 |
| Editor Actions tab | Check boxes; `email` etc. labelled "not available", no parameters | S5.1 |
| Settings screen for email/ntfy | Absent | S5.2 |

## S0 Setup

- [x] S0.1 Branch `claude/m6-actions` from 90ca022.
- [x] S0.2 Dependencies named by the spec: `aiosmtplib`, `keyring` (`uv add`).
- [x] S0.3 Baseline: 791 passed + 1 skipped at 90ca022.

## S1 Models and settings (`models.py`)

- [x] S1.1 Parameter models per action (`EmailParams`, `NtfyParams`, `WebhookParams`, `ExportParams`, `RunProgramParams`, `ScrapeParams`, `ScriptParams`) with template syntax checks.
- [x] S1.2 `ActionConfig` validates `params` for those types (HTTP 422 at create/patch); `run_program` needs `confirmed: true`.
- [x] S1.3 `Settings.notifications` (`SmtpSettings`, `NtfySettings`); API bodies for action jobs, test notifications and secrets.
- [x] S1.4 Regenerate `docs/openapi.json`.

## S2 Shared action plumbing (`engine/actions/`)

- [x] S2.1 `templating.py`: `{var}` expander (path/argument templates, `{date:<fmt>}`, safe file names) and the sandboxed Jinja2 environment.
- [x] S2.2 `content.py`: worker job that builds a change's alert content from blobs (added/removed text, plain diff, email-safe changes HTML, optional full highlighted page).
- [x] S2.3 `htmlmail.py`: email-safe HTML (inline styles only, table layout, no scripts/remote loads).
- [x] S2.4 `runtime.py`: per-engine content cache; `ActionConfigError` (final failure, no retries); privacy gate.
- [x] S2.5 `secrets.py`: `KeyringSecrets` (Windows Credential Manager) with `set`/`delete`; default store chosen by the engine entry point.

## S3 Actions

- [x] S3.1 `email` (`mailer.py`): SMTP over TLS via aiosmtplib, formats full/simple/changes_only, screenshot attachment, template, priority, one message per change (stable Message-ID).
- [x] S3.2 `ntfy`: server, topic, token from the secret store, priority; JSON publishing; private alerts carry no content.
- [x] S3.3 `webhook`: URL, method, Jinja2 JSON body, headers, optional bearer token.
- [x] S3.4 `export`: path template, `html` / `html_assets` / `text` / `diff_json`, atomic write.
- [x] S3.5 `run_program`: argv (no shell), `{var}` templates, files for new/old/changes, timeout kills, output logged, explicit confirmation.
- [x] S3.6 `scrape`: named fields (CSS, XPath, regex), one record per change appended to CSV / JSON Lines / XML; idempotent per change.
- [x] S3.7 `script`: declarative `check_folder(name)` and `pause_bookmark(minutes)`.
- [x] S3.8 `builtin.py` registers them; `webhook` becomes available, `pushover` stays unavailable.

## S4 Engine API

- [x] S4.1 `GET /action-jobs`, `POST /action-jobs/{id}/retry`, `DELETE /action-jobs/{id}` (the Problems list).
- [x] S4.2 `POST /notifications/test` (send a test email/ntfy/webhook with the saved settings).
- [x] S4.3 `GET/PUT/DELETE /secrets/{name}` (write-only values; the database keeps key names only).
- [x] S4.4 Signed-in bookmarks (auth/cookies/macro) default to `alert_privacy: private`.

## S5 UI wiring

- [x] S5.1 Editor Actions tab: per-action parameters dialog, implemented actions enabled, `run_program` confirmation, content-privacy confirmation for signed-in bookmarks.
- [x] S5.2 Notifications dialog (SMTP + ntfy, passwords to the secret store, send-test buttons). *Since the merge these controls live in M5's Settings window (Email and Notifications pages); see Merge.*
- [x] S5.3 Failed actions dialog (Problems) with retry/dismiss; main-window menu entries; status-bar message on a failed action. *Since the merge Retry and Dismiss are buttons in M5's Problems window; see Merge.*
- [x] S5.4 `ApiClient` support.

## S6 Tests

- [x] S6.1 Unit: param models, templating, content builder, email HTML, mailer (fake SMTP server), ntfy and webhook (HTTP server), export, program, scrape, script, secrets.
- [x] S6.2 Integration (engine + fixture site + fake SMTP/HTTP servers): each channel delivers exactly once per change; private bookmarks send no content; failing SMTP retries then a Problems entry; retry endpoint; test-notification endpoint; secrets endpoints.
- [x] S6.3 UI: action parameters dialog, notifications dialog, failed-actions dialog, editor round trip.

## S7 Finish

- [x] S7.1 `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (M6), `docs/openapi.json`.
- [x] S7.2 ruff (check and format), mypy `--strict`, schema check, full suite green: **1016 passed + 1 skipped** (default; was 791 + 1), 12 passed (`-m browser`). pytest exit code 0.
- [x] S7.3 Commit, push to `claude/m6-actions`, stop (no M7).

## M6 acceptance criteria → where they are proved

| Criterion (SPEC M6) | State |
| --- | --- |
| A failing SMTP server leads to retries, then a Problems entry | `test_m6_actions.py::test_a_failing_smtp_server_leads_to_retries_then_a_problems_entry_and_retry_works` (5 attempts at 1, 5, 30, 30 minutes, the `problem` event, `GET /action-jobs`, then Retry delivers); the list, retry and dismiss endpoints in `test_m6_api.py`; the dialog in `test_action_dialogs.py` |
| Private bookmarks send no page content | email, ntfy and webhook each have a test that searches everything sent for the page text, URL and summary (`test_private_email_...`, `test_private_ntfy_alert_...`, `test_webhook_body_template_and_private_alerts`); `test_action_content.py` for the template variables |
| A changes-only email renders correctly in Gmail and Outlook.com | **Manual check.** Tested: inline styles only, no `<style>`/classes/scripts/remote loads, table layout |
| An ntfy alert reaches the phone | **Manual check.** The request is tested against a local server |
| A macro logs into the fixture login site and detects a change behind it | **Not built** (logins half) |
| The portal experiment is run and recorded in DECISIONS.md | **Not built**; it is a 48-hour experiment on the owner's machine |
| Semi-attended sign-in checks immediately; an expired session yields one needs-login alert and pauses the bookmark | **Not built** (logins half) |
| The ACCESS HRA E-Notices bookmark never requests a notice PDF | **Not built** (needs the logins half and a manual check) |

Also covered beyond the criteria: each export format, programs without a shell and with a timeout,
scrape in three formats and once per change, `script`, screenshot attachments, alerts on a first
check, TLS arguments, the refusal to send a password in the clear, the secret store, the editor,
the Notifications dialog and the Problems dialog.

## Manual checks that cannot run in this sandbox

- A changes-only email in Gmail and Outlook.com (rendering).
- An ntfy alert reaching a phone (needs the real service).
- Real STARTTLS/implicit-TLS SMTP handshakes (the in-process server speaks plain SMTP; the TLS
  arguments are asserted on the aiosmtplib call).
- Windows Credential Manager (the tests use an in-memory keyring backend).


---

# M6 · Logins and proxies

Built on branch `claude/m6-logins` from `aadd380` (M0–M5, including M5 part 2) in parallel with
`claude/m6-actions` and merged into the integrated line last (see the final *Merge* part). This is
the half of SPEC's M6 that *M6 · Actions* above deferred: macros, semi-attended sign-in, cookie
import, per-bookmark proxies, per-host overrides and the portal experiment. Counts and file names
below are as they were on that branch; the merged results are in the final part.

## Scope decision (read this first)

| Unbuilt M6 item (from the actions branch's task list and SPEC) | State now |
| --- | --- |
| Check-Macro **replay** (goto, fill, click, select, press, wait_for, assert_text; secrets by key) | **Done**, real Chromium and HTTP-backed double |
| Check-Macro **recorder** ("Record macro" -> Playwright codegen -> steps JSON) | **Done** (converter and subprocess flow tested; **a real codegen window was not run here**) |
| **Session reuse** (macro runs only when a login signal appears) | **Done** |
| **Semi-attended sign-in** ("Sign in" window on the bookmark's profile, immediate check, daily prompt) | **Done** (window tested in new headless mode; **a visible window was not seen here**) |
| **Session expiry**: one needs-login alert, bookmark paused | **Done**, restart-safe |
| **Cookie import** (Netscape `cookies.txt` per bookmark) | **Done** (static and browser) |
| **Server auth** Basic / Digest from the secret store | **Done** (static and browser) |
| **Per-bookmark proxies** (HTTP, HTTPS, SOCKS5) and the global proxy | **Done** (with a stored proxy login). HTTPS-to-proxy TLS was not exercised |
| **Per-host overrides** | Concurrency and spacing overrides **already existed** (M5 part 2). Added what was missing: key normalisation, the scheduler stretching an over-limit host, the editor warning |
| **Stored-password login** rules (opt-in, 6 h, none after a failure) | **Done** (they are what the spec says must exist before any automated login) |
| **Portal experiment** | **The instrument is built and tested. The experiment itself has NOT been run**, and no findings are recorded. See `docs/DECISIONS.md` (*M6 · Logins and proxies*) |
| Never request an ACCESS HRA notice PDF | Proven against a fixture portal (request log). **The manual check against the real portal is still the owner's** |
| Signed-in bookmarks default to `alert_privacy: private` | **Done** for every way of becoming signed in (the actions branch only handles creation) |

Not in this session: the email/ntfy/webhook/export/run-program/scrape/script actions (the other half
of M6), and anything from M7.

## Audit: SPEC vs repository at aadd380

| SPEC requirement | State at `aadd380` | Task |
| --- | --- | --- |
| Basic / Digest auth, credentials from the keyring | `AuthConfig` model only; FTP read it, HTTP did not; `SecretStore` had no real store | S2.2, S1.3 |
| Cookie import, one `cookies.txt` per bookmark | `FetchConfig.cookies_file` field, never read | S2.3 |
| Global and per-bookmark proxy (HTTP, HTTPS, SOCKS5) | `fetch.proxy` honoured by httpx only; the browser used only the global proxy; no validation, no SOCKS (no `socksio`), no proxy login | S2.1 |
| Check-Macros; `macro` table | Table and `bookmark.macro_id` existed; no steps model, API, runner or UI | S3 |
| "Bookmarks with logins get a persistent context in `profiles\<id>\`" | `profiles/` folder existed; every page shared one ephemeral context | S3.2 |
| Macro runs only when a login signal appears | Nothing | S3.3 |
| Recording with Playwright codegen | Nothing | S3.6 |
| `needs_login` status | Enum value, UI colour and Problems line existed; **nothing ever set it** | S4 |
| Semi-attended sign-in; daily prompt; wipe session | Nothing | S4 |
| Per-host: 2 concurrent, >= 2 s, overridable | Done in `HostGate`/Settings (M5 part 2) | verified, S5 |
| "...the editor warns and the scheduler stretches that host's intervals evenly" | Explicitly deferred by M5 part 2 | S5.2, S5.3 |
| Portal experiment before any automated login | Nothing | S6 |

## S0 Setup

- [x] S0.1 Branch `claude/m6-logins` from **`aadd380`** (see DECISIONS: the checked-out branch was one commit behind it).
- [x] S0.2 Environment: Qt system libraries, `uv sync --extra ui`; baseline 956 passed + 1 skipped; `mypy --strict` clean.
- [x] S0.3 Read the actions branch's task list and SPEC; wrote this part and its decision log (`docs/DECISIONS.md`, *M6 · Logins and proxies*).

## S1 Models, secrets, schema

- [x] S1.1 `models.py`: `MacroStep` union (`GotoStep` ... `AssertTextStep`), `SecretRef`, `LoginSignal`, `MacroDef`/`MacroOut`, `LoginConfig`, proxy fields and `check_proxy_url`, `normalize_host`, new `Settings` fields, API bodies (cookies, login status, sign-in, recorder, experiments).
- [x] S1.2 A password field cannot be filled with an inline value (the model refuses it); secret names are restricted; a proxy address cannot carry a password.
- [x] S1.3 `engine/secrets.py` (keyring-backed store) **identical to the actions branch's file**; `Engine` default store as there; `GET/PUT/DELETE /secrets/{name}`, write-only.
- [x] S1.4 Migration `0002_logins.sql`: `login_state` (per-bookmark sign-in memory). Nothing else in the schema changes.
- [x] S1.5 Dependencies: `keyring` (named by the spec) and httpx's `socks` extra (SOCKS5 is in the spec). Flagged in DECISIONS.

## S2 Fetch layer

- [x] S2.1 `fetch/proxy.py`: bookmark proxy, else global; password from the secret store; static and browser.
- [x] S2.2 Static fetcher: Basic/Digest; refuses Basic over plain http to a non-local host; a 401 for a bookmark that sent credentials is `auth_rejected`.
- [x] S2.3 `fetch/cookies.py`: Netscape parser (HttpOnly prefix, spaces, BOM, expiry), httpx jar, Playwright cookies; a private session (client + jar) per signed-in bookmark; import / status / remove API (owner-only file).
- [x] S2.4 `FetchResult` gains `login_required`, `login_reason`, `macro_ran`, `macro_error`; `FetchRequest` gains `bookmark_id`, `login`.

## S3 Macros and browser sessions

- [x] S3.1 `fetch/macro.py`: the interpreter. Errors are built from the step, never from Playwright's text (which can echo a typed value).
- [x] S3.2 `BrowserManager`: `SessionSpec`, persistent profiles (own process), isolated contexts for cookies / proxy / server login, `launch_profile`; **session cookies saved beside the profile and restored** (Chromium drops them on close; found by a real-browser test).
- [x] S3.3 `fetch/login.py` + `BrowserFetcher`: signal on the live page; macro only when it shows, only if `plan.permit_macro()` allows; re-load and re-check afterwards.
- [x] S3.4 `Logins._permit`: stored-password double opt-in, one per `min_login_interval_s`, none after a failure; the attempt is recorded *before* the macro runs.
- [x] S3.5 `/macros` CRUD; bookmark validation (macro exists, browser method, a login signal exists).
- [x] S3.6 `logins/recorder.py`: codegen JSONL -> steps (a typed password is dropped, replaced by `{"secret": key}`), subprocess runner, `/macros/record`.

## S4 needs_login, sign-in, prompt

- [x] S4.1 Runner: `_login_gone` / `_needs_login`: status `needs_login`, `check_run` = error with a reason code, **no error counter**, **one** alert (only the transition), `next_due_at` NULL.
- [x] S4.2 Scheduler load and `schedule_bookmark` keep a `needs_login` bookmark paused across restarts; demand and Problems agree.
- [x] S4.3 `logins/signin.py`: visible window on the profile, watcher for "owner closed it", immediate check; scheduled checks skip (`signing_in`) while it is open.
- [x] S4.4 `logins/prompt.py`: the daily offer (never a keep-alive, never signs in by itself).
- [x] S4.5 Resume, wipe session, login status endpoints; signed-in bookmarks default to private (create and patch).
- [x] S4.6 Deleting a bookmark deletes its profile, imported cookies and in-memory jar (they are secrets), and closes an open sign-in window.

## S5 Per-host overrides

- [x] S5.1 Override keys normalised (`https://Example.COM:8443/x` -> `example.com`) in the model, in `PUT /settings` (so removal works under any spelling) and in the Settings table.
- [x] S5.2 `engine/hostload.py` + `schedule.next_due(stretch=)`: an over-limit host's interval checks wait `demand / ceiling` times as long, all alike; fixed-time and manual bookmarks are not stretched; paused bookmarks do not count as demand.
- [x] S5.3 Editor warning on the Schedule tab; Settings > Capacity shows the factor; `HostDemand.stretch`.

## S6 Portal experiment

- [x] S6.1 `logins/experiment.py`: the spec's protocol (probes at 10/20/30/60/120 min after a hand sign-in; two to four spaced password logins a day; challenge detection; pages saved; stop on the first failure), persisted, resumable; findings report as text for DECISIONS.
- [x] S6.2 API `/experiments`, CLI `pagewatch-cli portal-experiment start|list|report|cancel`.
- [ ] S6.3 **Run the experiment on the owner's machine and record the findings in DECISIONS.md.** Not doable here: needs the real portal, the owner's account and 48 hours. Until it is done `stored_password_logins` stays off.

## S7 UI

- [x] S7.1 Editor Login tab (`login_panel.py`), proxy login in Advanced, per-website warning on Schedule.
- [x] S7.2 Macros dialog and Record dialog (`macros.py`).
- [x] S7.3 Main window: *Sign in...* and *Macros* on the toolbar, the daily offer in the status bar; Problems: *Sign in...* / *Resume checks*, sentences for the new reasons.
- [x] S7.4 Settings: stored-password switch, host stretching, sign-in offer, proxy login, Intervals column.

## S8 Tests

- [x] S8.1 Unit: cookies, macro interpreter, login signal, recorder converter, models/migration/routing, static fetch with Basic/Digest/cookies/HTTP and SOCKS5 proxies.
- [x] S8.2 Integration (engine + fixture portal): macro acceptance, stored-password rules, semi-attended acceptance, restart, server auth, cookie import, ACCESS HRA request log, validation, privacy, per-host, prompt, recorder, experiment.
- [x] S8.3 Real Chromium (`-m browser`): the three acceptance criteria, session survival across a closed window, macros / profiles / cookies / Basic / proxy at the fetcher level.
- [x] S8.4 UI: Login tab, proxy login, Macros and Record dialogs, main window, Problems, Settings, editor warning.
- [x] S8.5 Mutation-checked: 15 behaviours broken one at a time, each caught (list in `docs/DECISIONS.md`).

## S9 Finish

- [x] S9.1 `docs/ARCHITECTURE.md`, a decision log (now the *M6 · Logins and proxies* section of `docs/DECISIONS.md`), `docs/openapi.json`.
- [x] S9.2 `ruff check .` and `ruff format --check src tests tools`, `mypy` (strict), full default suite, and the browser suite, all exit 0 (numbers in DECISIONS).
- [x] S9.3 Commit, push to `claude/m6-logins`, stop (no M7).

## M6 acceptance criteria -> where they are proved

| Criterion (SPEC M6) | Proof |
| --- | --- |
| A macro logs into the fixture login site and detects a change behind it | `tests/integration/test_m6_logins.py::test_a_macro_logs_into_the_fixture_site_and_detects_a_change_behind_it` (HTTP double, real fetcher and interpreter) and `tests/browser/test_m6_chromium.py::...` (real Chromium) |
| The portal experiment is run and its findings recorded before any stored-password login is enabled | **Not run.** The gate exists: `Settings.stored_password_logins` is off and a macro that fills a stored password refuses to run without it (`test_a_stored_password_login_needs_both_...`). The instrument: `tests/integration/test_m6_experiment.py` |
| A semi-attended sign-in checks immediately; an expired session yields exactly one needs-login alert and pauses the bookmark | `test_semi_attended_sign_in_checks_immediately_and_an_expired_session_alerts_exactly_once` (default suite) and `test_a_semi_attended_sign_in_checks_immediately_and_an_expired_session_alerts_once` (real Chromium) |
| The ACCESS HRA E-Notices bookmark never requests a notice PDF, per the request log (**manual check**) | `test_the_e_notices_list_is_watched_and_no_notice_pdf_is_ever_requested` in both suites against the fixture portal. **The check against the real portal is manual and not done.** |

Also covered beyond the criteria: Basic and Digest, rejected credentials pause instead of retrying,
imported cookies and their expiry, proxies (HTTP and SOCKS5, with and without a login), the
6-hour and after-a-failure rules, restart safety of `needs_login`, wrong-password macros that leak
nothing, host stretching and its off switch, the daily offer, recording.

## Manual checks that cannot run in this sandbox

- **The portal experiment** (48 hours, real account): `pagewatch-cli portal-experiment start <id>`.
- **A real `playwright codegen` window**: the converter is tested on JSONL shaped like Playwright's recorder
  output (checked against the installed driver's source); the window itself needs a display.
- **A visible sign-in window** on Windows with Edge: tests use new headless mode on the bundled Chromium.
- **Windows Credential Manager**: tests use an in-memory store (same as the actions branch).
- **ACCESS HRA and Housing Connect themselves**, including their terms of use (the spec asks for that review).
- **An HTTPS proxy** (TLS to the proxy) and CONNECT tunnelling to an https site: no TLS server in the fixtures.

The branch's notes on combining it with `claude/m6-actions` were acted on in the final *Merge · M6 logins* part below and are not repeated here.

---

# M7 · Power features

Built on branch `claude/m7-power-features` from `90ca022` (M0 to M4 complete, 791 tests green) in
parallel with M5, M6 and M8, and merged into the M5 line (see *Merge* at the end).

Scope, from SPEC.md *Milestones → M7*: Filter assistant in the UI, Follow-Links, merge pages,
virtual folders, import and export, reports, plugins with shipped examples, complete CLI.
Acceptance: Follow-Links on the fixture site creates child bookmarks within its depth and page
limits; each shipped plugin example passes its integration test. DECISIONS.md (M3) deferred the
Filter Assistant with Alt+select and the QWebChannel bridge to this milestone, and (M4) deferred
Follow-Links and merge pages.

## Audit: SPEC vs repository at 90ca022

| SPEC requirement | State in the repo | Task |
| --- | --- | --- |
| Filter assistant: Alt+select / right-click in Highlighted or New view, QWebChannel bridge reporting selection + DOM path, 5 choices, each with its Test filter result before saving | Missing; the viewer is a plain `QWebEngineView`, rendered HTML carries no DOM paths | F1 to F6 |
| `ui/web/bridge.js`, `ui/windows/filter_assistant.py` (layout) | Missing | F3, F4 |
| Follow-Links: include / exclude patterns, depth 1 default, at most 50 pages, child bookmarks in a generated folder that inherits the parent's settings, obeys robots.txt | Missing (`Trigger.FOLLOW` exists, unused) | L1 to L6 |
| Merge pages: follow a next-page selector or URL pattern up to N pages and concatenate before extraction | Missing | M1 to M3 |
| Virtual folders: saved queries (`folder.is_virtual`, `query_json`) | Column and API fields exist; the list endpoint ignores them, the UI hides them | V1 to V5 |
| Import: Netscape bookmarks HTML, URL list, CSV or XLSX (`name, url, folder, interval, keywords`), dedupe by normalised URL | Missing | I1 to I3 |
| Export: full config as versioned JSON, OPML for feeds, CSV list | Missing | I4 to I6 |
| Reports: Jinja2 templates to HTML, CSV, JSON, XML; built-ins changed-since-last-report, all-with-status, errors; on demand, CLI, or action | Missing (`jinja2` is a dependency, unused) | R1 to R5 |
| Plugins: seven hooks, `ctx` (read-only bookmark + `set_property`, rate-limited `http_get`, `log`, persistent `state`, regex / HTML helpers), trusted local code, raising hook = absent, > 5 s warns in the Check log, gate rule 8 | Missing (`plugins/` dir and `bookmark.plugin` column exist) | P1 to P8 |
| Shipped examples: price-range alert, page-not-changed-for-N-days, alert only when more than one new item, extract PDF links | Missing | P9 |
| CLI: `report`, `import`, `export` (+ plugin / follow / merge / virtual-folder options) | Missing | C1 to C4 |

## S0 Setup

- [x] S0.1 Branch `claude/m7-power-features` from 90ca022.
- [x] S0.2 `uv sync --extra ui`; Qt system libraries; baseline 791 passed + 1 skipped, ruff, mypy clean.

## S1 Shared models and helpers

- [x] S1.1 `models.py`: `FollowLinksConfig`, `MergePagesConfig` under `FetchConfig`; `VirtualQuery`; `ActionType.REPORT`; import / export / report / plugin / assistant API bodies.
- [x] S1.2 `engine/urls.py` (normalised URL key), `engine/kv.py` (namespaced key-value rows in the existing `setting` table; no migration).

## V Virtual folders

- [x] V1 `VirtualQuery` model + validation on folder create / patch (query required, no defaults, parent not virtual).
- [x] V2 `repo.bookmark_query` new filters (source type, method, plugin, error count, changed-within); list endpoint resolves `folder=<virtual id>`.
- [x] V3 Counts for virtual folders; bookmarks cannot be moved into a virtual folder; `POST /check` accepts one.
- [x] V4 UI: saved searches in the folder tree, "Save search as virtual folder", rename / delete.
- [x] V5 Tests: unit (query → rows), API, UI.

## I Import and export

- [x] I1 Parsers: Netscape HTML, URL list, CSV, XLSX, PageWatch JSON; interval parsing.
- [x] I2 Importer: folders from paths, dedupe by normalised URL (against the database and within the file), dry run, per-row errors.
- [x] I3 `POST /import`.
- [x] I4 Exporters: versioned JSON (no secrets), OPML (feeds), CSV.
- [x] I5 `GET /export`.
- [x] I6 UI: Import / Export dialog (File menu).
- [x] I7 Tests: parsers, round trip (export → import into a fresh engine), dedupe, API, UI.

## R Reports

- [x] R1 Built-in datasets and templates (changed since last report, all with status, errors) in HTML, CSV, JSON, XML.
- [x] R2 User templates (`<data>/reports/<name>.<fmt>.j2`), sandboxed Jinja2.
- [x] R3 "Since last report" marker, `POST /reports/{template}`, `GET /reports`.
- [x] R4 `report` action (writes the file when a change is detected).
- [x] R5 Tests.

## P Plugins

- [x] P1 `pagewatch/plugin_api.py` (`Verdict`, context types) and `engine/plugins.py` (discovery, loading with reload on change, hook calls with exception isolation and the 5 s warning).
- [x] P2 `ctx`: read-only bookmark, `set_property`, rate-limited `http_get`, `log`, persistent `state` (kv), `options`, helpers, `notify`.
- [x] P3 `before_check` (skip / change URL, headers, body), `merge_pages`; a skipped check is recorded as `skipped` / `plugin_skip`.
- [x] P4 `preprocess` (in `build_blocks` and the viewer so highlights stay aligned).
- [x] P5 Gate rule 8: `check_keywords` OR-ed with built-in keywords, `compare` force / deny / defer.
- [x] P6 `on_change`, `after_check`; state and property writes persisted.
- [x] P7 API: `GET /plugins`, `POST /plugins/install`; bookmark `plugin` validated.
- [x] P8 Slow / failing hooks recorded in `check_run.reason`.
- [x] P9 Shipped examples: `price_range`, `stale_page`, `single_new_item`, `pdf_links`, each with an integration test.
- [x] P10 Tests: loader, every hook, isolation, worker-process round trip.

## M Merge pages

- [x] M1 `MergePagesConfig` (next-page selector or URL pattern, `max_pages`).
- [x] M2 Worker jobs (next link, concatenation); runner integration; conditional GET off while merging.
- [x] M3 Tests (selector mode, pattern mode, limits, loop guard, one alert for a change on page 2).

## L Follow-Links

- [x] L1 `FollowLinksConfig` (include / exclude, depth, max pages, same host).
- [x] L2 `pipeline/links.py` (link extraction in the worker) and `engine/robots.py` (RFC 9309 behaviour, cached).
- [x] L3 `engine/follow.py`: discovery after a good check, generated folder (settings synced from the parent), children, seen-URL memory, lineage + one page budget per crawl root (per-root lock).
- [x] L4 Hook in the runner; `Trigger.FOLLOW` for the first check of a child.
- [x] L5 Tests: depth, page limit, include / exclude, robots, dedupe, deleted child not resurrected.
- [x] L6 Acceptance: Follow-Links on the fixture site stays within depth and page limits.

## F Filter assistant

- [x] F1 Engine: `data-pw-path` on rendered elements (`paths=true`) for the Highlighted and New views.
- [x] F2 Engine: `POST /bookmarks/{id}/filter-assist` (five choices, selector from DOM path, markers from the selection, text generalised, widen-to-parent).
- [x] F3 UI: `ui/web/bridge.js` + QWebChannel bridge (Alt+select, right-click, Ctrl+Shift+F); re-injected after every load (Qt injects `QWebEngineScript`s into the first document only); selected text excludes struck-through removals.
- [x] F4 UI: `ui/windows/filter_assistant.py` popup with live Test filter result per choice.
- [x] F5 Wiring: viewer → popup → `PATCH` → refresh.
- [x] F6 Tests: engine (each choice, markers, nesting, errors), bridge in a real `QWebEngineView`, popup, end to end against a real engine.

## C CLI

- [x] C1 `report <template> --out`, `import <file>`, `export <file>`.
- [x] C2 `plugin list|install`, `add --plugin / --follow-* / --merge-*`, `folder add --virtual --query`.
- [x] C3 Spec command list checked by a test (M5's `backup`, `restore`, `service` listed as expected from M5).
- [x] C4 Tests.

## E Editor and shell

- [x] E1 Bookmark editor: plugin selector, Follow-Links and merge pages (Advanced tab).
- [x] E2 Main window: Import / Export entry, saved-search actions, filter-assistant wiring.

## Z Finish

- [x] Z1 `docs/DECISIONS.md` (M7) (decisions, deviations, architecture additions, manual checks).
- [x] Z2 `docs/openapi.json` regenerated.
- [x] Z3 ruff, mypy `--strict`, full suite green: 1,009 passed + 1 skipped (was 791 + 1), 12 passed with `-m browser`.
- [x] Z4 Commit, push to `claude/m7-power-features`, stop.


---

# M8 · Packaging

Built on branch `claude/m8-packaging` from 90ca022 (M0-M4, 791 tests) in parallel with M5, M6 and
M7, and merged into the M5 line (see *Merge* at the end).

SPEC milestone M8: *PyInstaller build, Inno Setup installer, upgrade path with pre-migration
backup, short user guide.* Acceptance: *a clean Windows VM installs, autostarts at logon and
survives an upgrade with data intact.* Appendix row owned by M8: *portable install, multiple
instances, via `--data-dir`.*

Rules from the SPEC working agreement: deviations are logged (`docs/DECISIONS.md`, M8), ruff +
`mypy --strict` + the full suite green, one commit. New dependency: `pyinstaller` only (named in
the SPEC's technology table) in the `dev` dependency group, so it never reaches the runtime
install. Inno Setup is a Windows tool, not a Python dependency.

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
- [x] S1.6 Audit test: no bare `sys.executable` spawn outside `frozen.py` (fails loudly on the two M5 sites after the merge, with the fix in `DECISIONS.md (M8).md`).

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
- [x] S6.4 `docs/DECISIONS.md` (M8): deviations, merge notes for M5/M6/M7, what could not be run here.

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
  runs the packaged engine. *Applied and deleted when the branches were merged (see Merge).*
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
`docs/DECISIONS.md` (M8) lists each one.


---

# Merge · M6, M7 and M8 onto the M5 line

Branch `claude/focused-mccarthy-2a74te`, from `aadd380` (M5 complete). Scope, as instructed: merge
`claude/m6-actions` (`775f56c`), `claude/m7-power-features` (`bef0944`) and `claude/m8-packaging`
(`e7f6a3a`); apply M8's seam patch and delete it; wire the seams the decision logs name (Email
settings and Problems retry / dismiss into M5's windows, `POST /shutdown`, the scheduler stretching an
over-limit host *if the spec requires it*, which it does); fold the three decision logs and task lists
into `docs/DECISIONS.md`, `docs/ARCHITECTURE.md` and this file; `uv lock`, regenerate OpenAPI; ruff,
`mypy --strict` and pytest all exit 0. Decisions and the full account are in `docs/DECISIONS.md`,
*Merging M6, M7 and M8 into the M5 line*.

## S1 Merge

- [x] S1.1 Local branch was one commit behind `origin` (`a051864` vs `aadd380`): fast-forwarded.
- [x] S1.2 Merge M6: three conflicts (`ARCHITECTURE.md`, `ui/client.py`, `openapi.json`).
- [x] S1.3 Merge M7: eight conflicts, all of the "both sides added something" kind; M7's `_unavailable` stubs for M6's actions dropped.
- [x] S1.4 Merge M8: `uv.lock` only, regenerated with `uv lock` (adds PyInstaller and its dependencies).
- [x] S1.5 `git apply packaging/m5-frozen-seams.patch`, then the patch file deleted.

## S2 What the suite found after the merges

- [x] S2.1 Five of M5's action-queue tests used a bare `export` action, which M6 rejects at save time; the fake slot is now `sound`.
- [x] S2.2 `builtin.py` import order; `ruff format --check` failing on `docs/SPEC.md` at M5's own commit (Markdown excluded from the formatter, one stanza in `pyproject.toml`).
- [x] S2.3 The sandbox lacked `libEGL.so.1`, so no Qt test could import; installed for the session (see DECISIONS for the setup-script line).

## S3 Seams

- [x] S3.1 Email (SMTP) on Settings' Email page, ntfy on its Notifications page, as one tracked setting; secrets written first; a lone password counts as a change; "Send test" saves only those settings and rebases.
- [x] S3.2 Retry and Dismiss in the Problems window (failed-action rows only), using the `job_id` `GET /problems` already carries; M6's second toolbar and two dialogs removed, their status message moved to `MainWindow`.
- [x] S3.3 Tests ported and added (UI against a real engine); three mutations checked.
- [x] S3.4 `POST /shutdown`: token-gated, 202 then a clean exit 0 (so Task Scheduler does not restart), stop runs after the response is sent.
- [x] S3.5 `pagewatch-cli stop [--wait S]`: waits for the engine *process* to exit; nothing running is success. Real-process tests; zombie handling mutation-checked.
- [x] S3.6 `installer.iss` `StopEngine` calls `pagewatch-cli stop` first and keeps the hard kill as the fallback. Was not done at this merge (Inno Script that cannot be compiled or run here); done at the final integration, see the last part.
- [x] S3.7 Scheduler stretches an over-limit host's interval and adaptive gaps by demand ÷ ceiling (one factor for the whole host); the stored adaptive interval is not stretched; manual checks and fixed times are not affected.
- [x] S3.8 Stretch tests (evenly / quiet host untouched / queue 0 versus 30 without it / falls back / settings change / restart / throttling / failure) and schedule-math unit tests; two mutations checked.
- [x] S3.9 One rounding tolerance (`capacity.exceeds`) for "over its limit", shared by the Capacity page and the stretch; the page's warning text no longer says checks "fall behind".
- [x] S3.10 The editor's per-host warning, from the same spec sentence as S3.7. Not built at this merge (the request named only the scheduler); it arrived with M6 logins, see the last part.

## S4 Documents

- [x] S4.1 `docs/DECISIONS_M6.md`, `_M7.md`, `_M8.md` folded into `docs/DECISIONS.md` as `## M6`, `## M7`, `## M8` (stale statements corrected in place, noted where they changed) plus the merge section; the three files removed.
- [x] S4.2 `docs/ARCHITECTURE.md`: M7's module table and data flow, M8's packaging paragraph (both left out by their branches on purpose), `frozen.py` and `stretch.py`, the UI section, shutdown and exit codes.
- [x] S4.3 `TASKS_M6.md`, `_M7.md`, `_M8.md` consolidated into this file (one part per milestone; stale "parallel branch" intros replaced); the three files removed.
- [x] S4.4 `packaging/README.md`: the hard-kill note now says what exists and what is left.

## S5 Finish

- [x] S5.1 `uv lock`, `python tools/gen_openapi.py`: both stable on a second run.
- [x] S5.2 `ruff check`, `ruff format --check`, `mypy --strict`: all exit 0.
- [x] S5.3 `pytest`: exit 0, **1,544 passed, 1 skipped** (the pre-existing non-root skip; the sandbox is root), 17 deselected. Was 1,520 collected on the first merged run, with 6 failures that were fixed (S2.1, openapi).
- [x] S5.4 `pytest -m "browser or packaging"`: **15 passed**, including a real PyInstaller build of the merged tree (M8 asked for this after merging; it covers M6's `keyring` and M7's plugins in the bundle). Not run: `soak` (24 h) and everything that needs Windows.
- [x] S5.5 Commit and push to `claude/focused-mccarthy-2a74te`; stop (no pull request).

## Manual checks that cannot run in this sandbox

Those of M5 to M8 above all still apply. New: the Settings Email and Notifications pages and Problems
Retry / Dismiss on Windows with the real Credential Manager; `installer.iss` calling `pagewatch-cli
stop` on a real upgrade.

---

# Merge · M6 logins onto the integrated line

Branch `claude/focused-mccarthy-2a74te` (fast-forwarded from `a051864` to `8ab810a` first), then
`claude/m6-logins` (`c02224f`) merged in: the last piece of v1. Scope, as instructed: merge it;
deduplicate the secrets code and the signed-in privacy rule; combine the over-limit host stretching
with the logins branch's host-key normalisation and editor warning; check `0002_logins.sql` against
the upgrade and PyInstaller tests; make `installer.iss` stop the engine gracefully; fold the two
temporary logs into `docs/DECISIONS.md`, `docs/ARCHITECTURE.md` and this file; regenerate `uv.lock`
and `docs/openapi.json`; run every gate; push the branch and `main`. The full account is in
`docs/DECISIONS.md`, *Merging M6 · Logins and proxies*.

## S1 Merge

- [x] S1.1 Fast-forward the local branch (13 commits behind `origin`), `git fetch --all`, merge `origin/claude/m6-logins`: 17 conflicted files, all "both sides added something"; `secrets.py`, `tests/support/engine_thread.py`, `tests/conftest.py` merged silently as promised.
- [x] S1.2 `pyproject.toml`, `api/app.py`, `cli/main.py`, `ui/client.py`, `main_window.py`, `problems.py`, `bookmark_editor.py`, `browser.py`, `runner.py`, `core.py`: both sides kept.
- [x] S1.3 `uv.lock` and `docs/openapi.json` regenerated, not merged (lock +`socksio`; OpenAPI 36 -> 49 paths, 88 -> 110 schemas, no existing path changed).

## S2 Deduplicate (what git could not see)

- [x] S2.1 `models.py`: one `SECRET_NAME`, one `_secret_name` (an `@overload` pair, so both the required and the optional callers type-check), one `SecretIn`, one `SecretOut`.
- [x] S2.2 `/secrets/{name}`: the copy in `routes_logins.py` deleted; `routes_actions.py` keeps it. One operation each in OpenAPI.
- [x] S2.3 The UI client had the same three secret calls under two sets of names (`ruff` F811): the actions names kept, logins call sites renamed.
- [x] S2.4 Signed-in rule: the actions branch's `_default_private_when_signed_in` dropped; `logins.validate.default_private_when_signed_in` / `is_signed_in` is the only one (create *and* patch; counts a hand sign-in). The editor's own `_signed_in()` now calls it.

## S3 One host stretch

- [x] S3.1 `HostStretch` (`engine/stretch.py`) kept; `engine/hostload.py`, `Engine.host_load` and `Settings.host_load_refresh_s` removed.
- [x] S3.2 Ported onto it: `Settings.stretch_hosts` switch, `HostDemand.stretch` on the Capacity page, key normalisation (`normalize_host`), the editor's Schedule-tab warning.
- [x] S3.3 Found and fixed while porting: with the switch off the editor said "waiting 1.0 times as long" and the Capacity summary claimed a slowdown; both now say stretching is off (2 UI tests).
- [x] S3.4 `tests/integration/test_m6_hosts.py` repointed at `engine.host_stretch`; it and `test_host_stretch.py` pass together.

## S4 Packaging and the installer

- [x] S4.1 `0002_logins.sql` against `test_upgrade.py` (release 0.1.0's frozen data upgrades to v2 with the `pre-migration-v1` backup) and the bundle: passes. Added: `0002_logins.sql` in `package_data_files()`, and (real build) the bundle's migrations equal the source's.
- [x] S4.2 The macro recorder started `sys.executable -m playwright codegen`, which in the bundle is the engine itself. Now `frozen.playwright_command("codegen", ...)` (Playwright's bundled Node driver); a unit test pins the command line. The packaging guard against `sys.executable` also tripped once on the *docstring* of this fix and was reworded.
- [x] S4.3 `installer.iss`: `RunCli` moved above `StopEngine`, which calls `RunCli('stop --wait 30')` before `schtasks /End` and `taskkill /F /T`; both kept as the fallback. Two script tests. **Not compiled or run** (Inno Script, no Windows here).

## S5 Documents

- [x] S5.1 `docs/DECISIONS_M6_LOGINS.md` -> `## M6 · Logins and proxies` in `docs/DECISIONS.md` (stale statements marked **(Merge)**), plus `## Merging M6 · Logins and proxies`; earlier "installer not changed" and "editor warning not built" notes corrected in place.
- [x] S5.2 `TASKS_M6_LOGINS.md` -> `# M6 · Logins and proxies` and this part; the file's merge-notes table is not repeated (this part acted on it).
- [x] S5.3 `docs/ARCHITECTURE.md` (module rows, *Logins* section, UI paragraph, `frozen.py`, `stretch.py`, migrations), `packaging/README.md`. Both temporary files removed.

## S6 Finish

- [x] S6.1 `uv lock --check`, `tools/gen_openapi.py` (stable), `ruff check`, `ruff format --check src tests tools`, `mypy --strict`: all exit 0.
- [x] S6.2 `pytest`: **1,709 passed, 1 skipped**, 30 deselected, exit 0 on the last of several full runs; the earlier ones are in DECISIONS (a Bus error in UI teardown, a 200 ms timing budget missed once, a hang under `-s`): **none reproduced, none explained**.
- [x] S6.3 `pytest -m browser`: **24 passed**. `pytest -m packaging`: **4 passed** (a real PyInstaller build, run and smoke-tested).
- [x] S6.4 Commit, push to `claude/focused-mccarthy-2a74te`, push the same commit to `main`.

## Manual checks that cannot run in this sandbox

Those of M5 to M8 and M6 above all still apply. **The portal experiment has not been run** (48 hours,
the owner's ACCESS HRA / Housing Connect accounts): until `docs/DECISIONS.md`'s findings table is
filled in, `Settings.stored_password_logins` stays off. Also: a real `playwright codegen` window
(from source and from the bundle), a visible sign-in window on Windows with Edge, an HTTPS proxy,
`installer.iss` stopping a running engine on a real upgrade, and the ACCESS HRA "never request a
notice PDF" check against the real portal.
