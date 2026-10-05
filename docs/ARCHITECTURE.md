# PageWatch architecture

PageWatch is two (three) processes that share one data folder:

```
pagewatch-engine  (always on, in the user's session; the only writer, the only network client)
   ├─ asyncio event loop: scheduler, fetchers, local API, action jobs
   ├─ writer thread: owns the single SQLite write connection
   ├─ reader threads: read-only SQLite connections
   ├─ worker processes (spawn): parse → filter → hash → diff → gate
   └─ tray thread (Windows)
pagewatch-ui      (on demand; PySide6; a client of the local API)
pagewatch-cli     (scripting; a client of the local API)
```

Data folder (`%LOCALAPPDATA%\PageWatch`, or `--data-dir`):
`pagewatch.db` (WAL), `blobs/ab/cd/<sha256>.zst`, `profiles/`, `logs/engine.log`,
`backups/`, `plugins/`, `engine.lock`, `engine.mutex`.

## Layers (as built)

| Module | Role |
| --- | --- |
| `models.py` | Pydantic models: every JSON column and every API body. No engine imports. |
| `engine/clock.py` | `Clock` protocol, `SystemClock`, `FakeClock`. No other module reads time directly. |
| `engine/paths.py` | `DataDir` layout and `--data-dir` / env / platform default resolution. |
| `engine/logs.py` | structlog JSON logs, rotating file (10 MB × 5), secret redaction, runtime DEBUG toggle. |
| `engine/store/db.py` | Migrations (one transaction, backup first); `Database` = writer thread + reader pool. |
| `engine/store/blobs.py` | Content-addressed, zstd-compressed, atomic blob store; safe across processes. |
| `engine/workers.py` | Process pool (spawn) with a thread-mode twin for tests; rebuilds after a crash. |
| `engine/settings.py` | Global settings: one row per changed key in `setting`. |
| `engine/instance.py` | Single-instance guard (named mutex / flock) and `engine.lock`. |
| `engine/core.py` | `Engine`: owns db, blobs, pool, event bus, settings; lifecycle and `/health`. |
| `engine/api/` | FastAPI app, ASGI guard (token, no Origin, loopback Host), `/events` WebSocket. |
| `engine/main.py` | Entry point `pagewatch-engine`: guard → logging → engine → API → lockfile → wait. |
| `engine/store/repo.py` | All SQL. `commit_check` applies one check atomically; `mark_read`; keyset-paged list query. |
| `engine/config.py` | `FolderCache` and effective config: `global defaults ← folder chain ← bookmark overrides`. |
| `engine/bookmarks.py` | Create / patch / delete service: validation, inheritance, scheduler sync, events. |
| `engine/schedule.py` | Pure schedule math: interval, times, adaptive, days/window, jitter, battery; DST-correct. |
| `engine/hostgate.py` | Per-host concurrency cap, request spacing, `Retry-After` back-off. |
| `engine/scheduler.py` | Due-time heap, per-host ready queues, pools, pause/resume, manual & hotsite priority. |
| `engine/runner.py` | One check end to end; retry-once; error counting; the atomic commit; events. |
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
| `engine/actions/` | `toast` (coalesced), `builtin` (action registry), `queue` (durable job runner). |
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
* The scheduler wakes at least every 30 s; the action queue every 60 s.
* The API list endpoints are keyset-paged (`limit` ≤ 500).

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
