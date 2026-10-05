# PageWatch architecture

PageWatch is two (three) processes that share one data folder:

```
pagewatch-engine  (always on, in the user's session; the only writer, the only network client)
   ├─ asyncio event loop: scheduler, fetchers, local API, action jobs, power/connectivity,
   │     nightly maintenance, resource guard
   ├─ writer thread: owns the single SQLite write connection
   ├─ reader threads: read-only SQLite connections
   ├─ worker processes (spawn): parse → filter → hash → diff → gate (killed and rebuilt past a time limit)
   ├─ watchdog thread: logs the loop's stack when the event loop stalls
   ├─ power-window thread (Windows): sleep / resume / log-off messages
   └─ tray thread (Windows)
pagewatch-ui      (on demand; PySide6; a client of the local API)
pagewatch-cli     (scripting; a client of the local API)
```

Data folder (`%LOCALAPPDATA%\PageWatch`, or `--data-dir`):
`pagewatch.db` (WAL), `blobs/ab/cd/<sha256>.zst`, `profiles/`, `logs/engine.log`,
`backups/` (automatic and manual zips, pre-migration and pre-restore copies), `restore/` (a staged
restore, only between a restore request and the next start), `plugins/`, `engine.lock`, `engine.mutex`.

## Layers (as built)

| Module | Role |
| --- | --- |
| `models.py` | Pydantic models: every JSON column and every API body. No engine imports. |
| `engine/clock.py` | `Clock` protocol, `SystemClock`, `FakeClock`. No other module reads time directly. |
| `engine/paths.py` | `DataDir` layout and `--data-dir` / env / platform default resolution. |
| `engine/logs.py` | structlog JSON logs, rotating file (10 MB × 5), secret redaction, runtime DEBUG toggle. |
| `engine/store/db.py` | Migrations (one transaction, backup first); `Database` = writer thread + reader pool; `backup_sync` (online backup on a connection of its own). |
| `engine/store/blobs.py` | Content-addressed, zstd-compressed, atomic blob store; safe across processes. |
| `engine/workers.py` | Process pool (spawn) with a thread-mode twin for tests; rebuilds after a crash; a job past `worker_job_timeout_s` is killed and raises `WorkerTimeout`. |
| `engine/settings.py` | Global settings: one row per changed key in `setting`; plus engine *state* (`_state.*` rows, e.g. last backup) that is never a setting. |
| `engine/instance.py` | Single-instance guard (named mutex / flock) and `engine.lock`. |
| `engine/core.py` | `Engine`: owns db, blobs, pool, event bus, settings; lifecycle, `/health`, online/battery state, `catch_up`, backup and restore entry points. |
| `engine/api/` | FastAPI app, ASGI guard (token, no Origin, loopback Host), `/events` WebSocket. |
| `engine/main.py` | Entry point `pagewatch-engine`: guard → logging → engine → API → lockfile → wait; exit code 3 starts a replacement unless `--supervised`. |
| `engine/store/repo.py` | All SQL. `commit_check` applies one check atomically; `mark_read`; keyset-paged list query. |
| `engine/config.py` | `FolderCache` and effective config: `global defaults ← folder chain ← bookmark overrides`. |
| `engine/bookmarks.py` | Create / patch / delete service: validation, inheritance, scheduler sync, events. |
| `engine/schedule.py` | Pure schedule math: interval, times, adaptive, days/window, jitter, battery; DST-correct. |
| `engine/hostgate.py` | Per-host concurrency cap, request spacing, `Retry-After` back-off. |
| `engine/scheduler.py` | Due-time heap, per-host ready queues, pools, pause/resume, manual & hotsite priority; dispatch *holds*, battery parking, `catch_up`. |
| `engine/runner.py` | One check end to end; retry-once; error counting (verifying connectivity first; nothing counted offline); the atomic commit; events. |
| `engine/fetch/` | `FetchResult`/`FetchError`; `static.py` (httpx, HTTP/2, conditional GET, body cap). |
| `engine/fetch/select.py` | `route_for` (URL scheme + `check_method` -> fetcher), `method_kind` (the name `check_run.method` gets once the content is known), `FetcherSet`. |
| `engine/fetch/browser.py` | `BrowserManager` (one shared browser, generations, idle close, recycle, Edge -> bundled Chromium) and `BrowserFetcher`; `screenshot.py` is its PNG-taking subclass. |
| `engine/fetch/localfile.py`, `ftp.py`, `feed.py` | Files and folders (mtime+size shortcut, listing table), FTP/FTPS (aioftp), feeds (static fetch + optional enclosure download). |
| `engine/secrets.py` | `SecretStore` interface (passwords are looked up by key name; the keyring implementation is M6). |
| `engine/pipeline/` | `extract` (bytes→blocks), `special` filters, `differs` (pluggable) + `diff` (two-stage), `gate`, `render`, `core` (the worker entry points). |
| `engine/pipeline/filters.py` | Cosmetic/watch/ignore filters (marks, regions, ranges, text spans, digit masks). |
| `engine/pipeline/keywords.py` | Keyword language: parser and evaluator (`page()`, `num()`, `[same_block]`, `[near N]`, NOT). |
| `engine/pipeline/autofilter.py` | False-positive -> proposed ignore rules, verified; `data/volatile_patterns.yaml`. |
| `engine/changes.py` | Test filter and false-positive operations behind `routes_changes.py`. |
| `engine/pipeline/viewer.py` | HTML views: text view, in-page highlight (offset mapping through raw text nodes), sanitising, CSP. |
| `engine/pipeline/detect.py` | Resource classification and JavaScript-shell detection (`browser_reason`). |
| `engine/pipeline/sources.py` | `resolve_kind` (explicit type, content type, extension, magic bytes), conversion dispatch, `view_bytes` (what the viewer re-reads). |
| `engine/pipeline/documents.py`, `feeds.py` | PDF/DOCX/XLSX and RSS/Atom -> deterministic HTML. |
| `engine/pipeline/records.py` | Records sources: JSONPath subset, row filter, JSON/CSV rows -> one block per record, new/changed/removed events. |
| `engine/pipeline/screenshot.py` | The pixel diff (grayscale, ignore rectangles, threshold, regions, overlay PNG). Pillow only. |
| `engine/tray.py` | `TrayController` (state, menu) + Windows `pystray` adapter; icons drawn with Pillow. |
| `engine/actions/` | `toast` (coalesced), `builtin` (action registry), `queue` (durable job runner: per-change order, retries, restart-safe). |
| `engine/power.py` | `PowerBackend` (Windows window / `NullPowerBackend`), `PowerMonitor` (resume detection, battery, keep-awake), `Connectivity` (probe, offline mode). |
| `engine/maintenance.py` | Daily backup (03:00) and nightly retention (03:30) "or at the next wake"; last-run state persisted. |
| `engine/guard.py` | `ResourceGuard` (RSS of the process tree → recycle browser → exit 3; hourly `metric` samples) and `LoopWatchdog` (stack dump on a stalled loop). |
| `engine/store/retention.py` | Version pruning, blob mark-and-sweep, disk cap, `check_run`/`metric` purge, WAL checkpoint. |
| `engine/store/backup.py` | Backup zips, validation, staged restore applied at start-up. |
| `engine/api/routes_maintenance.py` | `POST /backup`, `POST /restore`. |
| `cli/service.py` | Task Scheduler task XML and `schtasks` wrapper for `service install\|uninstall\|status`. |
| `cli/` | `pagewatch-cli`: a synchronous client of the local API. |

## Rules that hold everywhere

* The event loop never runs `sqlite3` calls, parsing, hashing or diffing.
* Each check's writes are one `BEGIN IMMEDIATE … COMMIT` on the writer thread.
* A blob is written (atomic rename) **before** any row references it.
* Times are stored as fixed-width UTC strings (`2026-10-05T13:10:00.000000Z`) so string order is time order.
* Only the engine writes data. The UI and CLI go through the API.

## Security model

The API binds `127.0.0.1` on a random port. Every request needs `Authorization: Bearer <token>`
(token regenerated at every start, in `engine.lock`, owner-only). Requests with an `Origin`
header or a non-loopback `Host` are rejected. WebSockets send the token in their first message.

## Life of a check (as built)

```
Scheduler ──due──▶ ready queue (per host) ──host gate + pool──▶ Runner.run(id, trigger)
   │                                                               │
   │                                  db.read: bookmark + latest/anchor versions
   │                                  db.write: check_run(started)           ◀── crash here = "interrupted"
   │                                  route_for(url, source_type, check_method) → one fetcher:
   │                                       static (httpx; If-None-Match / If-Modified-Since) · feed
   │                                       browser / screenshot (BrowserManager; the DOM, plus a PNG)
   │                                       file (mtime+size → not_modified) · ftp (size+mtime)
   │                                  WorkerPool ▶ process_check(job)
   │                                       0. resolve_kind(source_type, content type, URL, magic bytes)
   │                                          auto-detection (first check of an `auto` page): a shell → "needs_browser"
   │                                          └─▶ the runner re-fetches with the browser, persists `browser`, runs again
   │                                       1. raw hash == latest.raw_hash?      → unchanged_raw (stop)
   │                                          (screenshot method: screenshot hash == latest's)
   │                                       2. convert (PDF/DOCX/XLSX/feed → HTML; records → blocks) → parse → blocks → special filters
   │                                          filtered hash == latest's?         → unchanged (stop)
   │                                       3. bad-fetch gate (min chars/blacklist/whitelist) → rejected (stop)
   │                                       4. write raw + blocks blobs; diff latest→new (+ anchor→new)
   │                                          (screenshot method: pixel diff latest.png→new.png instead, below
   │                                           threshold → unchanged and nothing stored; else PNG + overlay blobs)
   │                                       5. gate verdict (ignore-removed, keywords, thresholds; records: events)
   │                                  db.write: commit_check  (ONE transaction)
   │                                       version, change, latest/baseline/anchor pointers, check_run.method,
   │                                       check_method (auto → browser), next_due_at + adaptive interval, action_job rows
   ◀── RunResult(next_due, next_trigger, browser) ──  events: check_finished, change_detected, bookmark_updated
                                              ActionQueue.kick() ▶ toast / sound / open / mark_read ...
```

### Sources

| Source | Fetcher | Becomes | `check_run.method` |
| --- | --- | --- | --- |
| web page | `static` | HTML | `static` |
| JavaScript page | `browser` (rendered DOM, same-origin iframes inlined) | HTML | `browser` |
| visual monitoring | `screenshot` (browser + 1366×900 PNG) | picture compared by pixels; text stored too | `screenshot` |
| PDF / DOCX / XLSX | `static`, `file` or `ftp` | HTML (page / paragraphs and tables / one table per sheet) | `document` (`file`, `ftp` keep theirs) |
| RSS / Atom | `static` (`feed` when downloading enclosures) | one `<li>` per entry | `feed` |
| JSON / CSV records | `static`, `file` or `ftp` | one block per record, keyed by ID | `records` |
| local file / folder | `file` | text, document or a listing table | `file` |
| FTP / FTPS | `ftp` | file as above, or a listing table | `ftp` |

### The browser

```
BrowserFetcher / ScreenshotFetcher ──ensure()──▶ BrowserManager ──launch plan──▶ Playwright
      page(): ≤ 3 at once, a fresh page in the shared context (a second one for "don't verify TLS")
      plan order: browser_executable → channel msedge (--headless=new) → bundled Chromium (installed on demand)
      generation = one launched browser; retired after 500 pages or 3 crashes in a row, closed when drained
      idle for 10 minutes (engine clock) → closed;  nothing launches → state `unavailable`, retry in 5 minutes
```

`/health.browser_state` is `stopped`, `running` or `unavailable`.

### The three pointers

| Pointer | Moves when | Used for |
| --- | --- | --- |
| `latest_version_id` | every good fetch whose filtered text changed (alerted or not) | what every check compares against; keeps the unchanged shortcut working |
| `baseline_version_id` | mark read (and the first check) | start of the viewer's diff: unread changes accumulate until read |
| `gate_anchor_version_id` | an alert, or mark read (and the first check) | cumulative word thresholds compare the new page with this |

### Bounds worth knowing

* Worker work per check is bounded: diff budget 2 s, 20,000 blocks, 5,000 tokens per replace run,
  6×10⁸ alignment cells; exceeding any sets `degraded` on the diff.
* The scheduler wakes at least every 30 s; the action queue at least every 60 s, and at the next
  retry when one is nearer.
* The API list endpoints are keyset-paged (`limit` ≤ 500).

## Unattended operation (M5)

Everything in this section runs only when the engine is built with `enable_unattended` (the real
entry point does; unit tests opt in), so a test never probes a real network by accident.

### What can stop scheduled dispatch

Manual "Check now" always runs; scheduled checks wait while **any** of these holds:

| Reason | Set by | Cleared by |
| --- | --- | --- |
| AutoWatch paused | user / tray / timed pause | resume / expiry |
| offline | `Connectivity`: a probe failed | a probe succeeds (then catch-up) |
| `resume` hold | `PowerMonitor` after a sleep | the network answered (or the window ran out) *and* the catch-up was planned |
| `startup` hold | `Engine.start` | start-up probe and catch-up done |
| `catchup:<why>` hold | network or AC power returning | its catch-up was planned |
| `saver` hold | Windows battery saver, if the option is on | battery saver off |
| per bookmark: `on_battery: pause` | being on battery | AC power (the bookmark is still overdue, so it catches up) |

A hold is only released **after** the catch-up has re-timed what is overdue; releasing first would
let the scheduler start every overdue bookmark at once as plain `schedule` checks.

### Sleep, resume, catch-up

```
heartbeat every 5 s (engine clock)  ─┐ wall − monotonic drift > 30 s
Windows WM_POWERBROADCAST            ─┼─▶ PowerMonitor.handle_resume (one per wake: 10 s debounce)
heartbeat that fires > 30 s late     ─┘     hold "resume" → wait for the network (probe / 5 s, ≤ 2 min)
                                              → Engine.catch_up → release
Engine.catch_up: every overdue bookmark, hotsites first then longest overdue, gets ONE check
  (trigger `catchup`) spread over min(5 min, count × 1 s); a bookmark whose days/window forbid
  "now" is moved to its next allowed start instead.
```

A check that fails with a network-looking error (DNS, connection, timeout; never for a local file)
first asks `Connectivity.verify()`. If the probe fails the engine goes **offline**: dispatch stops,
the run is recorded as `skipped` / `offline:<kind>`, no error counter moves, the bookmark stays
due, and a recovery loop probes every 15 s. The same catch-up runs when the network is back.

### Maintenance

```
MaintenanceLoop (60 s poll, first look 2 min after start)
   backup   : due when the latest 03:00 (local) is newer than the last backup → zip in backups\
   retention: due when the latest 03:30 (local) is newer than the last run
              prune versions → mark references → sweep unreferenced blobs unused for 24 h
              → disk cap → delete check_run / metric older than 30 days → WAL checkpoint
```

Retention's safety rests on one invariant: a blob's modification time is its *last use* (a
writer's dedupe hit refreshes it), and the sweep deletes only blobs unreferenced **and** unused for
`blob_grace_h`. Whole-table scans (retention, backup) use short-lived connections so they leave no
copy of the database in a pooled reader's page cache.

### Restore

`POST /restore` validates the zip (manifest, integrity check, schema not newer, no path traversal)
and parks it in `restore/`; the engine exits with code 3. At the next start, **before** the
database is opened, `apply_pending_restore` copies the current database to `backups\pre-restore-*`
and swaps the restored one in. Under Task Scheduler (`--supervised`) the task restarts the engine;
otherwise the engine starts its own replacement (refused if it was itself started that way less
than a minute ago).

### Exit codes

0 normal quit · 1 error · 2 another engine owns the data folder · 3 restart me (restore applied,
memory limit). Task Scheduler restarts on any non-zero code, every minute.

## The UI (`pagewatch.ui`, optional `ui` extra)

```
MainWindow ── FolderTree (built-ins + folders + counts) ─┐
           ── QTableView ◀── BookmarkListModel ◀── ApiClient (httpx, worker threads) ──▶ engine API
           ── ViewerPanel (history list, toggles, tab bar) ── one shared QWebEngineView
                                  └─ Screenshot diff tab: the overlay PNG from the engine, fitted to the pane
           ── EventStream (QWebSocket /events) ──▶ refresh rows / counts / viewer
dialogs:   BookmarkEditor · AddBookmarkDialog · FalsePositiveDialog
```

Closing the window stops only its own timers and WebSocket; the engine is a separate process and
keeps checking. Lazy list: the engine pages (`/bookmarks` keyset cursor), the model appends.
