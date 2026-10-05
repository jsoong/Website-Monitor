# PageWatch — Technical Specification

Oct 5, 2026 · @Jason

## Overview

PageWatch is a local Windows desktop application that checks web pages and other sources on a schedule, ignores noise, highlights what changed, and acts on it. It reimplements the core of Aignesberger's [WebSite-Watcher](https://www.aignes.com/features.htm) (v26.4) for one user on one machine, running unattended around the clock. "PageWatch" is a working name.

### Goals

1. Monitor about 1,000 watched sources ("bookmarks") and stay usable up to 10,000, each with its own schedule from 1 minute to weekly.
2. Run unattended for weeks. Survive sleep and resume, network loss, crashes and reboots without losing state or flooding alerts.
3. Keep false positives low through a layered filter pipeline and a "Test filter" preview.
4. Show changes clearly: in the app, a highlighted diff of the last-read version against the latest; in notifications, the change that triggered the alert.
5. Keep all data local in one folder. No account, no cloud dependency, no telemetry.

### Non-goals for v1

- Mobile app, cloud sync, multi-user or multi-machine operation.
- Windows Service (session 0) mode. The engine always runs in the user's session.
- AI-generated summaries of changes.
- Pushover and webhook actions (planned after v1).
- Keep-alive pings to hold signed-in portal sessions open.
- Facebook, X and Instagram scraping, CAPTCHA solving, proxy rotation.
- A custom scripting language. Extensibility is Python plugins plus a CLI.

### Success criteria

| Measure                                   | Target                                                       |
| ----------------------------------------- | ------------------------------------------------------------ |
| Design load                               | 1,000 bookmarks; usable at 10,000                            |
| Engine memory, browser closed             | ≤ 250 MB RSS at 1,000 bookmarks; ≤ 400 MB at 10,000          |
| Engine memory, browser running            | ≤ 1.2 GB RSS                                                 |
| Idle CPU, no checks due                   | < 1% average                                                 |
| Static check throughput                   | ≥ 20 checks/s across hosts (1,000 bookmarks at 1 minute need 16.7/s) |
| Bookmark list with 10,000 rows            | Scroll, sort and filter respond in < 200 ms                  |
| Recovery after resume from sleep          | All overdue bookmarks queued within 60 s, staggered          |
| Continuous uptime                         | 14 days without restart; RSS growth < 10%                    |
| False positives on the golden test corpus | 0                                                            |

## Key decisions and assumptions

Build it in Python 3.12 as two processes: an always-on engine that alone touches the network and writes data, and a PySide6 UI that can open and close freely.

| Area          | Choice                                                       | Why                                                          |
| ------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| Language      | Python 3.12, managed with uv                                 | Mature scraping, parsing and document libraries; matches the owner's existing PySide6 tools |
| Process model | `pagewatch-engine` (always on) + `pagewatch-ui` (on demand) + `pagewatch-cli` | Closing or crashing the UI never stops monitoring or corrupts data |
| Autostart     | Task Scheduler task at user logon, restart on failure        | A Windows Service runs in session 0: no toasts, no per-user credential store, awkward browser profiles |
| Engine ↔ UI   | FastAPI + uvicorn on 127.0.0.1, random port, bearer token in a lockfile | Debuggable with curl; WebSocket push for live status         |
| Storage       | SQLite (WAL) for metadata; zstd-compressed snapshots in a content-addressed blob folder | Small database, free dedupe of unchanged content, one-folder backup |
| Static fetch  | httpx (async, HTTP/2)                                        | Pooling, per-request timeouts, proxies, conditional GET      |
| Dynamic fetch | Playwright driving the system Microsoft Edge (msedge channel), with Playwright's bundled Chromium as fallback: one shared browser, started lazily, closed after 10 idle minutes | JavaScript pages and screenshots without a resident browser. Edge ships with Windows 10 and 11, so the only bundled Chromium is the UI's |
| HTML parsing  | lxml + cssselect                                             | Fast; CSS and XPath for filters                              |
| Diff          | Pluggable Differ: block-hash alignment, then word diff inside changed runs; implementation chosen by an M1 benchmark (difflib, cdifflib, rapidfuzz opcodes, fast-diff-match-patch) | A native library speeds the common case; size caps and a time budget bound the worst case, which any LCS-style diff has |
| Documents     | pdfminer.six, python-docx, openpyxl                          | Permissive licences (PyMuPDF is faster but AGPL)             |
| Feeds         | feedparser                                                   | RSS 0.9x–2.0 and Atom                                        |
| GUI           | PySide6 with QWebEngineView for the in-page diff view and the Filter-Assistant | Renders page HTML faithfully and provides the JavaScript bridge (QWebChannel) that Alt+select needs. QTextBrowser has no JavaScript or DOM, and embedding WebView2 in a Qt window needs custom focus, keyboard and DPI handling |
| Notifications | windows-toasts for toasts; pystray tray icon owned by the engine | Alerts arrive while the UI is closed                         |
| Secrets       | keyring (Windows Credential Manager)                         | Passwords never stored in SQLite or exports                  |
| Templates     | Jinja2 sandboxed environment                                 | Emails, exports, reports, webhooks                           |
| Packaging     | PyInstaller one-folder build + Inno Setup installer; the UI's QtWebEngine is the one bundled Chromium | No system Python required                                    |

### Assumptions

- Host is a Windows 10/11 x64 laptop (Ryzen 7 8845HS). It sleeps, changes networks and runs on battery.
- One user, one install. Data lives in `%LOCALAPPDATA%\PageWatch\` unless `--data-dir` is given.
- Monitoring targets public pages or the owner's own accounts, at polite request rates.

### Owner decisions

- **Alert channels:** Windows toast, email and ntfy. Pushover and webhooks move to after v1.
- **Scale:** design for about 1,000 bookmarks; 10,000 is the extreme upper bound to test against. The shortest interval in practice is 1 minute.
- **Signed-in sites:** ACCESS HRA and NYC Housing Connect were named as must-haves. Revised in review: rely on the portals' own alerts, watch Housing Connect's public listings, and keep signed-in checks optional and semi-attended. See Portal strategy under Fetch layer.
- **AI summaries:** not in scope.
- **Service mode:** not planned. The engine always runs in the user's session.
- **Keyword AND scope:** terms may appear anywhere in a check's changes by default; same-block and proximity scopes are opt-in.
- **Word thresholds:** cumulative since the last alert by default; per-check available per bookmark.

## System architecture

All monitoring runs in one always-on engine process; the UI is a separate client that reads and edits through a local API, so it can close without stopping anything.

[embed: node/0a540514-d04d]

Every check runs top to bottom through the eight stages. Compare reads the last version from the store, and only the engine ever writes to it.

### Components

| Component     | Module                | Responsibility                                               |
| ------------- | --------------------- | ------------------------------------------------------------ |
| Scheduler     | `engine/scheduler.py` | Due-time heap, adaptive intervals, catch-up, paused state    |
| Check runner  | `engine/runner.py`    | Worker pools, per-host limiter, timeouts, retries, `check_run` records |
| Fetchers      | `engine/fetch/`       | One per source type; each returns a `FetchResult`            |
| Pipeline      | `engine/pipeline/`    | Extract, filter, diff, gate, render; runs in the worker pool |
| Store         | `engine/store/`       | SQLite access, blob store, retention, backup                 |
| Actions       | `engine/actions/`     | Durable job queue and action implementations                 |
| Power monitor | `engine/power.py`     | Resume detection, connectivity probe, battery state          |
| Local API     | `engine/api/`         | REST endpoints and the `/events` WebSocket                   |
| Tray          | `engine/tray.py`      | Status icon, menu, hidden window for power messages          |
| Plugin host   | `engine/plugins.py`   | Loads plugin modules and calls their hooks                   |
| UI            | `ui/`                 | PySide6 client of the API                                    |
| CLI           | `cli/`                | Scripting, reports, service install                          |

### Concurrency model

- One asyncio event loop handles network I/O (httpx, Playwright), scheduling, the API and job orchestration. It does no parsing, hashing or diffing.
- A process pool (default 3 workers on an 8-core machine, configurable) runs the whole pipeline: raw bytes to blocks, hashes, diffs and the gate verdict. Workers read earlier versions from the blob store themselves, so only small results cross process boundaries.
- A dedicated writer thread owns the SQLite write connection, because `sqlite3` calls block. The loop hands it write operations through a queue; readers use their own connections.
- The tray runs in its own thread and talks to the loop through `call_soon_threadsafe`.
- A bookmark moves through `queued → fetching → processing → done | error`; each transition is broadcast on `/events`.

## Data model

One SQLite database (`pagewatch.db`, WAL mode, `foreign_keys=ON`) holds configuration and history. Page content lives in compressed blob files referenced by SHA-256.

```sql
CREATE TABLE folder (
  id            INTEGER PRIMARY KEY,
  parent_id     INTEGER REFERENCES folder(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  is_virtual    INTEGER NOT NULL DEFAULT 0,
  query_json    TEXT,                       -- saved search for virtual folders
  defaults_json TEXT NOT NULL DEFAULT '{}'  -- bookmark defaults inherited by children
);

CREATE TABLE bookmark (
  id                  INTEGER PRIMARY KEY,
  folder_id           INTEGER REFERENCES folder(id) ON DELETE SET NULL,
  name                TEXT NOT NULL,
  url                 TEXT NOT NULL,          -- http(s)://, ftp://, file:///
  source_type         TEXT NOT NULL DEFAULT 'auto', -- auto|html|feed|pdf|docx|xlsx|ftp|file|folder|image|binary|records
  check_method        TEXT NOT NULL DEFAULT 'auto', -- auto|static|browser|screenshot
  enabled             INTEGER NOT NULL DEFAULT 1,
  priority            INTEGER NOT NULL DEFAULT 0,   -- 1 = hotsite
  schedule_json       TEXT NOT NULL,
  fetch_json          TEXT NOT NULL DEFAULT '{}',   -- headers, POST body, UA, proxy, timeouts, browser options
  filter_json         TEXT NOT NULL DEFAULT '{}',   -- cosmetic, watch, ignore, special
  gate_json           TEXT NOT NULL DEFAULT '{}',   -- keywords, thresholds, black/whitelist
  highlight_mode      TEXT NOT NULL DEFAULT 'standard', -- standard|exact|table
  actions_json        TEXT NOT NULL DEFAULT '[]',   -- actions plus alert_privacy
  macro_id            INTEGER REFERENCES macro(id) ON DELETE SET NULL,
  plugin              TEXT,
  info1 TEXT, info2 TEXT, info3 TEXT, note TEXT,
  status              TEXT NOT NULL DEFAULT 'new',  -- new|ok|changed|error|needs_login|disabled
  unread              INTEGER NOT NULL DEFAULT 0,
  consecutive_errors  INTEGER NOT NULL DEFAULT 0,
  current_interval_s  INTEGER,                      -- adaptive state
  next_due_at         TEXT,                         -- ISO-8601 UTC
  last_checked_at     TEXT,
  last_changed_at     TEXT,
  latest_version_id      INTEGER, -- newest good fetch; every check compares against this
  baseline_version_id    INTEGER, -- version at last mark-read; the viewer's diff starts here
  gate_anchor_version_id INTEGER, -- version at last alert or read; cumulative thresholds compare here
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX ix_bookmark_due ON bookmark(enabled, next_due_at);

-- Every *_hash column holds the SHA-256 of the uncompressed content, which is also
-- the blob's file name. Content never goes into SQLite.
CREATE TABLE version (
  id              INTEGER PRIMARY KEY,
  bookmark_id     INTEGER NOT NULL REFERENCES bookmark(id) ON DELETE CASCADE,
  fetched_at      TEXT NOT NULL,
  raw_hash        TEXT,          -- raw content (HTML, PDF, feed XML) in the blob store
  blocks_hash     TEXT NOT NULL, -- normalized block list (JSON) in the blob store
  filtered_hash   TEXT NOT NULL, -- hash of the comparison text; no blob
  screenshot_hash TEXT,          -- PNG in the blob store
  http_status INTEGER, content_type TEXT,
  etag TEXT, last_modified TEXT, -- sent back for conditional GET
  byte_size INTEGER, word_count INTEGER,
  pinned          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_version_bm ON version(bookmark_id, fetched_at DESC);

CREATE TABLE change (
  id                 INTEGER PRIMARY KEY,
  bookmark_id        INTEGER NOT NULL REFERENCES bookmark(id) ON DELETE CASCADE,
  old_version_id     INTEGER REFERENCES version(id), -- what the gate compared against: previous latest, or the anchor in cumulative mode
  new_version_id     INTEGER NOT NULL REFERENCES version(id),
  detected_at        TEXT NOT NULL,
  added_words INTEGER, removed_words INTEGER, changed_blocks INTEGER,
  checks_accumulated INTEGER NOT NULL DEFAULT 1, -- checks spanned by a cumulative alert
  keyword_hits_json  TEXT,
  diff_hash          TEXT,   -- the gate diff (old -> new) in the blob store; what the alert reports
  summary            TEXT,   -- first 200 chars of added text
  feedback           TEXT,   -- NULL | 'false_positive'
  read_at            TEXT
);

CREATE TABLE view_diff_cache (  -- one row per bookmark: the viewer's baseline -> latest diff
  bookmark_id         INTEGER PRIMARY KEY REFERENCES bookmark(id) ON DELETE CASCADE,
  baseline_version_id INTEGER NOT NULL,
  latest_version_id   INTEGER NOT NULL,
  diff_hash           TEXT NOT NULL,  -- diff ops (JSON) in the blob store
  created_at          TEXT NOT NULL
);

CREATE TABLE check_run (       -- retained 30 days
  id INTEGER PRIMARY KEY,
  bookmark_id INTEGER NOT NULL REFERENCES bookmark(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL, finished_at TEXT,
  trigger  TEXT NOT NULL,  -- schedule|manual|catchup|retry|follow
  method   TEXT NOT NULL,  -- static|browser|screenshot|document|feed|ftp|file|records
  outcome  TEXT NOT NULL,  -- first|unchanged|changed|suppressed|error|skipped
  reason   TEXT,           -- e.g. keyword_miss, blacklist, below_threshold, http_503
  duration_ms INTEGER, bytes INTEGER
);

CREATE TABLE macro (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  steps_json TEXT NOT NULL, login_signal_json TEXT);
CREATE TABLE action_job (id INTEGER PRIMARY KEY,
  change_id INTEGER NOT NULL REFERENCES change(id) ON DELETE CASCADE,
  action_index INTEGER NOT NULL, action_type TEXT NOT NULL,
  status TEXT NOT NULL,  -- queued|done|failed
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
  UNIQUE(change_id, action_index));
CREATE TABLE metric (ts TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL);
CREATE TABLE setting (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE schema_version (version INTEGER NOT NULL);
```

### Rules

- **Three pointers per bookmark.** `latest_version_id` is the newest good fetch: every check compares against it, and it advances on every fetch the gate doesn't reject as bad. `baseline_version_id` is the last-read version: the viewer's diff runs from baseline to latest, so unread changes accumulate until marked read. `gate_anchor_version_id` is the version at the last alert or read: cumulative word thresholds compare against it.
- **Two kinds of diff.** Each `change` row stores the gate diff that triggered it (`old_version_id` → `new_version_id`); alerts report it and the history view steps through it. The viewer's default diff, baseline → latest, is computed on demand in the worker pool and cached as one `view_diff_cache` row per bookmark, replaced whenever either pointer moves.
- **JSON columns** are validated by Pydantic models in `pagewatch/models.py`, the single source of truth shared by engine, UI and CLI.
- **Blob store:** `blobs\ab\cd\<sha256>.zst`, named by the SHA-256 of the uncompressed content. The `*_hash` columns hold these names, never the content. Writes are atomic (temp file, fsync, rename) and happen before the DB row that references them. Because `raw_hash` is computed before compression, the pre-parse shortcut compares hashes without reading any file.
- **Retention:** keep every version a pointer references, pinned versions, and the last 20 changed versions per bookmark (configurable), under a global disk cap (default 10 GB) that prunes the oldest unpinned versions first. Change rows go with their pruned versions. A nightly job deletes unreferenced versions and blobs, and check_run rows older than 30 days.
- **Migrations:** numbered SQL files applied at startup in one transaction, after an automatic backup.

## Fetch layer

Each bookmark resolves to one fetcher that returns a `FetchResult`. Everything after the fetch is fetcher-agnostic.

```python
@dataclass
class FetchResult:
    final_url: str
    status: int | None
    headers: dict[str, str]
    content_type: str
    body: bytes                 # raw bytes; browser method returns serialized DOM HTML
    screenshot_png: bytes | None
    elapsed_ms: int
    not_modified: bool          # HTTP 304, or unchanged mtime+size for files
    error: FetchError | None    # kind: timeout|dns|tls|http|too_large|browser|parse
```

| Fetcher    | Handles                                                      | Behaviour                                                    |
| ---------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| static     | HTTP(S) HTML, JSON, text                                     | httpx; conditional GET (ETag, Last-Modified); ≤ 10 redirects; charset from header, then meta, then charset-normalizer |
| browser    | JavaScript-rendered pages                                    | Playwright page on system Edge (msedge channel) in new headless mode, bundled Chromium as fallback; waits for `load` + `delay_after_load_s`; scrolls `scroll_count` times (800 px, 500 ms apart); optional synthetic mouse and keyboard input; returns `page.content()` with same-origin iframes inlined |
| screenshot | Visual monitoring                                            | Browser fetch, then full-page or clipped PNG at a fixed 1366×900 viewport; animations disabled, device scale 1 |
| document   | PDF, DOCX, XLSX (by content type or extension)               | Converted to HTML: PDF text per page; DOCX paragraphs and tables; XLSX one table per sheet, capped at 5,000 rows |
| feed       | RSS, Atom                                                    | feedparser; one block per entry keyed by entry id or link; optional enclosure download |
| ftp        | FTP and FTPS files and listings                              | aioftp; a listing becomes a table of name, size, modified time |
| file       | Local files and folders                                      | mtime+size shortcut first; files go through the document or text path; a folder becomes a listing table, recursion optional |
| records    | JSON or CSV feeds: APIs, open-data datasets, a page's own JSON | Rows keyed by an ID field; each record becomes one block (see Records sources) |

### Method auto-detection

New bookmarks default to `check_method='auto'`. The first check runs static. If readable text is under 200 characters, or the body is a script-only app shell, the engine re-runs it in the browser. It then persists `browser` and logs why.

### Browser management

- One browser instance, launched on first need. It is the system Microsoft Edge via Playwright's msedge channel; if Edge is missing or fails to launch, the engine falls back to Playwright's bundled Chromium, downloaded on first use. It allows at most 3 concurrent pages and closes after 10 idle minutes.
- Bookmarks with logins get a persistent context in `profiles\<bookmark_id>\`. All others share one ephemeral context.
- Images, media and fonts are blocked except for the screenshot method.
- 45 s hard timeout per page. Recycle the browser after 500 pages or 3 consecutive crashes.

### Logins and Check-Macros

- **Server auth:** HTTP Basic and Digest, credentials from keyring.
- **Cookie import:** a Netscape `cookies.txt` per bookmark.
- **Check-Macros:** ordered Playwright steps run before the check: `goto`, `fill`, `click`, `select`, `press`, `wait_for` (selector or ms), `assert_text`. Secret values are `{"secret": "<keyring key>"}`, never inline.
- **Recording:** "Record macro" launches Playwright codegen and converts its output into steps JSON for review.
- **Session reuse:** the macro runs only when a login signal appears (a selector, or a redirect to the login URL), so most checks skip logging in.

### Portal strategy

The two named portals are covered mostly without signing in: their own alerts handle account events, and PageWatch watches Housing Connect's public listings. Signed-in checks remain an optional, semi-attended feature, not a v1 must-have.

| Need                                                         | Covered by                                                   |
| ------------------------------------------------------------ | ------------------------------------------------------------ |
| ACCESS HRA case events: new e-notices, requested documents, appointments | The portal's own email and text alerts (set in the ACCESS HRA profile) plus the mobile app's push notifications |
| Housing Connect account events                               | The text-message opt-in on the Housing Connect account       |
| New Housing Connect lotteries                                | PageWatch: the [public listings page](https://housingconnect.nyc.gov/PublicWeb/search-lotteries) via the browser fetcher (static fetch returns an empty app shell), or the JSON feed that page loads, as a `records` source keyed by lottery ID |
| Monthly cross-check of lottery data                          | PageWatch: the NYC Open Data [Advertised Lotteries on Housing Connect](https://catalog.data.gov/dataset/advertised-lotteries-on-housing-connect-by-lottery) dataset, refreshed monthly; it omits mini-lotteries and Mitchell-Lama lotteries |

- **Semi-attended sign-in (optional):** "Sign in" opens a visible browser window on the bookmark's persistent profile. The owner logs in, including any one-time code, and the engine checks immediately while the session is fresh. A daily prompt offers to sign in again.
- **No keep-alive:** the engine never pings to hold a session open. Session idle and absolute timeouts are measured, not assumed.
- **Session expiry:** a check that lands on a login signal sets `needs_login`, alerts once, and pauses the bookmark until the owner signs in again.
- **Stored-password login (opt-in, only after the portal experiment):** allowed only if spaced test logins show no one-time code or CAPTCHA on routine logins. At most one automated login per 6 hours, and none after a failure.
- **Never open ACCESS HRA e-notices:** watch only the E-Notices list page. Opening a notice may clear its unread marker, and paperless enrollees must read each notice themselves within 30 days.
- **Private alerts:** signed-in bookmarks default to `alert_privacy: private`.
- **Matching fingerprint:** checks reuse the sign-in profile in the same browser's new headless mode, so the checking browser resembles the one that signed in.
- **Portal experiment (before any automated login is built):** over 48 hours on the owner's machine, sign in once by hand, then check at 10, 20, 30, 60 and 120 minutes to measure the idle timeout. Try two to four spaced password logins a day to see whether a code or CAPTCHA appears, and record any challenge pages.
- Review each portal's terms of use before turning on automated checks.

### Politeness and network

- Global pools: 32 static, 3 browser. Per host: 2 concurrent, ≥ 2 s apart, overridable per host.
- Honour `Retry-After` on 429 and 503 and back off the whole host.
- Default user agent is a current desktop Chrome string; per-bookmark override.
- Global and per-bookmark proxy (HTTP, HTTPS, SOCKS5).
- Body cap 20 MB, then error `too_large`. TLS verification on unless a bookmark opts out.
- robots.txt is not enforced for single URLs the user chose; Follow-Links crawling obeys it.

### Multi-page sources

- **Follow-Links:** discover links matching include and exclude patterns, to depth 1 by default and at most 50 pages. New matches become child bookmarks in a generated folder that inherits the parent's settings.
- **Merge pages:** follow a next-page selector or URL pattern up to N pages and concatenate them before extraction.

### Records sources

A `records` bookmark fetches JSON or CSV and treats each row as one record keyed by an ID, so alerts name what was added or changed instead of showing a text diff.

- **Config:** URL, format (JSON or CSV), JSONPath to the row array, ID field, optional row filter (for example `borough in [MN, BK]` or `lottery_status = Active`), and the fields to watch.
- **Events:** `new` (an unseen ID), `changed` (a watched field differs, such as status becoming Active) and `removed` (an ID disappears). Each bookmark chooses which events alert.
- **Pipeline fit:** each record becomes one block whose text is its watched fields in a stable order, so the normal gate, history and viewer apply unchanged.
- **First use:** Housing Connect lotteries keyed by `lottery_id`, from the listings page's own JSON feed if one is found in the browser's developer tools (unofficial; it may change without notice), with the monthly Open Data dataset as a cross-check.

## Normalization and filtering

Raw content becomes an ordered list of text blocks, and the same filters reduce both versions before comparison, so only meaningful differences survive.

### Pipeline order

1. **Parse:** bytes to an lxml HTML tree. Documents and feeds already arrive as HTML.
2. **Cosmetic filters:** delete nodes matching CSS or XPath selectors (cookie banners, nav, ads). A built-in cookie-banner selector list is on by default.
3. **Block extraction:** each block-level element (p, li, tr, h1–h6, pre, blockquote, div with direct text) becomes `Block{path, kind, text, links, images}`. Script, style, noscript and comments are dropped; whitespace collapses; table cells join with " | ".
4. **Watch filters:** if any exist, keep only blocks inside the watched regions.
5. **Ignore filters:** remove matched elements, ranges or text spans.
6. **Special filters:** global toggles, applied last.
7. **Comparison text:** blocks joined by newlines. `filtered_hash` = SHA-256 of that text.

### Filter types

| Type          | Matches                                                      | Example                                     |
| ------------- | ------------------------------------------------------------ | ------------------------------------------- |
| `selector`    | Whole elements by CSS or XPath; ignore or watch              | `aside.related`, `//div[@id='price']`       |
| `between`     | Content between a start and an end marker; either side may be "start of page" or "end of page" | from "Latest news" to "Archive"             |
| `text`        | Spans in block text by literal, wildcard (`*`, `?`) or regex | `Updated * ago`, `\d+ views`                |
| `number_mask` | Replaces digits with `#` inside matched blocks               | Counters that change while the layout stays |

### Special filters

| Filter                                  | Default | Effect                                                  |
| --------------------------------------- | ------- | ------------------------------------------------------- |
| Compare text only (ignore HTML tags)    | On      | Off compares raw HTML source                            |
| Ignore case                             | On      | Lower-cases before comparison                           |
| Normalize Unicode and whitespace (NFKC) | On      | Stops invisible-character alerts                        |
| Ignore dropdown and list-box entries    | On      | Drops `<option>` text                                   |
| Sort content                            | Off     | Sorts blocks first, so reordering alone is not a change |
| Ignore removed content                  | Off     | Only additions and edits can trigger                    |
| Watch link URLs                         | Off     | Appends extracted hrefs as blocks                       |
| Watch image URLs                        | Off     | Appends extracted image sources as blocks               |

### Automatic filters

The user can flag any change as a false positive. The engine then proposes ignore rules from the changed spans:

- A span matching a known volatile pattern becomes a `text` regex scoped to its block's selector. Patterns include dates, times, "N minutes ago", counters next to views or visitors, currency amounts and hex tokens of 16+ characters.
- Any other span becomes a `selector` ignore on its smallest containing element.
- The proposal re-runs the comparison and is saved only if the false positive disappears and the user confirms.

Volatile patterns ship in `data/volatile_patterns.yaml` so they can grow without code changes.

### Test filter

`POST /bookmarks/{id}/test-filter` runs the full pipeline on the stored baseline and latest raw content with a candidate config. It returns both filtered texts and the diff, and persists nothing.

## Change detection and diffing

Every good fetch whose filtered text differs from the latest version is stored and becomes the new latest; the gate decides only whether it alerts. The user always sees the diff from the last-read version to the latest.

### Check sequence

1. If the fetch reports `not_modified`, or the raw-content hash equals the latest version's `raw_hash`, record `unchanged` and stop before parsing.
2. Run normalization and filtering in a worker process. If `filtered_hash` equals the latest version's, record `unchanged` and stop.
3. Diff latest → new for keyword rules and per-check thresholds. Also diff gate anchor → new when a cumulative threshold is set.
4. Run the gate (next section).
5. Unless the gate rejected the fetch as bad (error status, too short, blacklist, whitelist miss), store the version and advance latest. This keeps the unchanged shortcut working and stops one change from looking new on every later check.
6. On pass: create a `change` row, set the bookmark `changed` and unread, move the gate anchor to the new version, and queue actions. The change row records what the gate compared against (previous latest, or the anchor in cumulative mode) and stores that diff for the alert.
7. The first successful check of a bookmark stores the baseline with outcome `first`. It alerts only if the global "notify on first check" setting is on.

### Diff algorithm

- **Pluggable `Differ`:** the implementation is chosen by an M1 benchmark on the golden corpus. Candidates: difflib, cdifflib, rapidfuzz opcodes and fast-diff-match-patch.
- **Stage 1, block alignment:** match the sequences of block-text hashes into equal, insert, delete and replace runs. Blocks are hashed by text, not DOM position, so wrapper-element changes don't disturb alignment.
- **Stage 2, word diff:** inside each replace run, tokenize on Unicode word boundaries with punctuation kept as tokens, then diff the token lists.
- **Moves:** a block deleted in one place and inserted elsewhere with the same hash is a `mov` op and is not counted as a change in Standard mode.
- **Bounded worst case:** a replace run above 5,000 tokens skips the word diff and renders as a block replacement. A diff that exceeds a 2 s time budget falls back to block-level output and sets `diff_degraded`. A native library speeds the common case, but only these bounds limit a full-page rewrite.
- **Performance target:** set from the M1 benchmark rather than assumed. Diffs run only when the filtered hash changed, so they are off the checks-per-second path.

### Highlight modes

| Mode               | Behaviour                                                    | Best for                          |
| ------------------ | ------------------------------------------------------------ | --------------------------------- |
| Standard (default) | Block alignment, moves ignored, word diff in changed blocks  | News and listing pages            |
| Exact              | Token diff across the whole text; moves count as changes     | Pages where single numbers change |
| Table              | Rows are blocks; a changed cell highlights its whole row. Option: numeric-only changes highlight just the cell | Price lists, data tables          |

### Diff format (cached in `diff_hash`)

```json
{
  "ops": [
    {"t": "eq",  "old": [0, 12], "new": [0, 12]},
    {"t": "ins", "new": [13, 14]},
    {"t": "del", "old": [13, 13]},
    {"t": "rep", "old": [14, 14], "new": [15, 15],
     "tokens": [["eq", "Price "], ["del", "$19"], ["ins", "$17"]]},
    {"t": "mov", "old": [30, 30], "new": [5, 5]}
  ],
  "stats": {"added_words": 4, "removed_words": 2, "changed_blocks": 2},
  "degraded": false
}
```

### Rendering

- **In-page view:** re-inject `<ins class="pw-add">` and `<del class="pw-del">` into the new version's HTML at each block's DOM path. Scripts are stripped and remote resources stay off unless the user enables images.
- **Text view:** normalized blocks with inline marks. It always works and is reused for "changes only" emails.
- **Deletions:** shown struck through in place, or listed in a side panel, per a viewer toggle.
- **Changed images:** compare image source lists; a changed image gets an outline.

### Which diff appears where

| Where                  | Diff shown                            | Source                                                       |
| ---------------------- | ------------------------------------- | ------------------------------------------------------------ |
| Viewer, default        | Last read → latest, everything unread | `GET /bookmarks/{id}/diff`, computed on demand in the worker pool; one cached pair per bookmark |
| Viewer, change history | Each alert's own gate diff            | `GET /changes/{id}/render`                                   |
| Notifications          | The diff that triggered the alert     | The change row's `diff_hash`                                 |

### Screenshot comparison

1. Load both PNGs at the same viewport and convert to grayscale.
2. Blank out ignore rectangles (screenshot filters).
3. Mark pixels whose absolute difference exceeds 24/255, then dilate and group into regions.
4. Report a change if changed pixels exceed `min_ratio` (default 0.2%) or page height changed by more than 5%.
5. Save an overlay PNG with red boxes around each region.

## Alert gating

The gate turns a detected difference into an alert only if every configured rule passes, and it records the reason whenever it suppresses one.

### Rule order (first failure wins)

| #    | Rule                                                         | Compared against                                            | Default                  | On failure               | Advances latest? |
| ---- | ------------------------------------------------------------ | ----------------------------------------------------------- | ------------------------ | ------------------------ | ---------------- |
| 1    | HTTP status is 2xx or 304                                    | —                                                           | Always on                | Error path, not a change | No               |
| 2    | Readable characters ≥ `min_chars`                            | New page                                                    | 0 (off); UI suggests 100 | `too_short`              | No               |
| 3    | No blacklist phrase present                                  | New page                                                    | Empty list               | `blacklist`              | No               |
| 4    | A whitelist phrase present, if a whitelist exists            | New page                                                    | Empty list               | `whitelist_miss`         | No               |
| 5    | Something added or modified, if "ignore removed content" is on | Latest                                                      | Off                      | `removed_only`           | Yes              |
| 6    | A keyword rule matches                                       | Changes since latest; `page()` terms use the whole new page | No keywords              | `keyword_miss`           | Yes              |
| 7    | Changed words ≥ `min_changed_words`; skipped after a keyword hit | Gate anchor (`cumulative`) or latest (`per_check`)          | 0 (off)                  | `below_threshold`        | Yes              |
| 8    | Plugin `check_keywords` / `compare` hooks                    | Hook's choice                                               | None                     | Hook's reason            | Yes              |

Rules 1–4 reject bad fetches, which never advance any pointer, so an error page can't become the comparison base. Every other outcome stores the version. A keyword hit skips the word threshold, because naming a keyword already says what matters. Keywords are never matched against text removed by ignore filters.

### Keyword syntax

One rule per line; lines are OR-ed. Matching is case-insensitive.

| Syntax                  | Meaning                                                      | Example                                      |
| ----------------------- | ------------------------------------------------------------ | -------------------------------------------- |
| `word`                  | Substring in the changed text                                | `watch` matches "WebSite-Watcher"            |
| `"word"`                | Whole word                                                   | `"watch"`                                    |
| `a + b`                 | All terms appear somewhere in this check's changes (AND)     | `"lottery" + "manhattan"`                    |
| `... [same_block]`      | All terms inside one changed block                           | `"approved" + "subsidy" [same_block]`        |
| `... [near N]`          | All terms within N words of each other in the changes        | `"approved" + "subsidy" [near 30]`           |
| `-term`                 | Rule fails if the term appears in the changes (NOT)          | `laptop + -refurbished`                      |
| `page(term)`            | Context: the term anywhere on the new page, changed or not   | `page("RTX 4090") + num(\$([\d,.]+)) < 1200` |
| `regex(...)`            | Regular expression                                           | `regex(in\s+stock)`                          |
| `num(regex) <op> value` | First capture parsed as a number, then compared with <, <=, >, >=, = | `num(\$([\d,.]+)) < 1200`                    |
| `... #color`            | Highlight colour for this rule                               | `sale #red`                                  |

A separate highlight-only list colours terms in the view without gating. Matches are stored in `keyword_hits_json` and shown in the bookmark list.

A rule needs at least one term outside `page()`, so it fires only on change. Because keywords compare against changes since the latest version, a value that flips back ("in stock" after "out of stock") alerts with no special option. A check's changes span the whole interval since the previous check, including sleep, so broad AND rules on busy pages can pair unrelated items; use `[near N]` there. For one product's price, a watch filter on the price element plus `num()` is simplest.

### Word thresholds

| Mode                   | Label in the editor                                     | Compares          | Suits                                               |
| ---------------------- | ------------------------------------------------------- | ----------------- | --------------------------------------------------- |
| `cumulative` (default) | "Alert when changes since the last alert total N words" | Gate anchor → new | Slow edits and small additions that matter together |
| `per_check`            | "Alert when one check changes N words"                  | Latest → new      | Pages where only one large edit matters             |

- Cumulative alerts say how many checks the change accumulated over.
- Cumulative results don't depend on check frequency or sleep. Per-check results do: the same edits pass when they land in one check and fail when spread across several.
- In-place churn such as dates and counters doesn't accumulate, because each comparison is between two snapshots. Rotating multi-item regions plateau at their own size; ignore them with a filter.
- There is no time-based reset: a silent reset would turn slow real edits into misses.

### Errors

- Transient errors (timeout, DNS, connection reset, 5xx) retry once after 60 s before counting.
- After `error_threshold` consecutive errors (default 3) the bookmark turns `error` and notifies once, not on every check.
- The counter resets on the next success or when the user opens the bookmark.
- While the engine is offline (see Scheduler), failures are not counted at all.

### Notification coalescing

Changes detected within one 30 s window produce one summary toast ("5 bookmarks changed"). Per-bookmark actions such as email still run once per change.

## Scheduler (AutoWatch)

One asyncio loop keeps every enabled bookmark's `next_due_at` in a min-heap and hands due checks to bounded worker pools; because due times live in SQLite, a restart resumes exactly where it stopped.

### Schedule config

```json
{
  "mode": "interval",
  "interval_s": 3600,
  "times": ["09:00", "17:30"],
  "days": ["mon", "tue", "wed", "thu", "fri"],
  "window": {"start": "07:00", "end": "23:00"},
  "adaptive": {"min_s": 900, "max_s": 86400, "factor": 1.5},
  "jitter_pct": 10,
  "on_battery": "normal"
}
```

| Field            | Meaning                                                      |
| ---------------- | ------------------------------------------------------------ |
| `mode`           | `interval`, `times` (fixed local clock times), `adaptive`, or `manual` (only on demand) |
| `days`, `window` | Optional limits that apply to every mode; outside them, the next due time moves to the next allowed start |
| `adaptive`       | Unchanged check: interval × factor, up to max. Change detected: reset to min. A real change (a new filtered hash versus latest) resets to min even when it misses its keywords; because latest advances, one change resets the interval once, not on every later check |
| `jitter_pct`     | Random ± spread so checks don't align on the hour            |
| `on_battery`     | `normal`, `slow` (interval × 4) or `pause`                   |

Folders carry defaults; a bookmark overrides any field. The minimum interval is 60 s, with no lower setting.

### Dispatch

- At most one in-flight check per bookmark.
- Per-host limiter and the global pools (32 static, 3 browser) from the Fetch layer.
- Manual "Check now" (bookmark, folder or all) and hotsites jump the queue.
- A backlog above 200 items raises a status-bar warning.
- Startup waits 30 s (configurable) before the first dispatch.
- The AutoWatch running or paused state, including "paused until", survives restarts.
- Times are stored in UTC; `times` and `window` are evaluated in the local zone via `zoneinfo`, so DST shifts are handled.

### Capacity at 1,000 to 10,000 bookmarks

- **Demand:** each pool's load is the sum of 1/interval over its bookmarks. 1,000 bookmarks at 1 minute need 16.7 checks/s; at 15 minutes, 1.1 checks/s. Settings shows current demand per pool and per host.
- **Per-host ceiling:** with 2 s spacing, one host serves at most 30 checks a minute. When a host's demand exceeds its limit, the editor warns and the scheduler stretches that host's intervals evenly instead of letting the queue grow.
- **Browser pool:** 3 pages taking several seconds each is the scarcest resource. Settings shows browser demand against capacity, measured from the rolling average check time, and warns above 80%.
- **At 10,000 bookmarks:** the heap and SQLite indexes handle it unchanged. The UI list pages through the API with a cursor, and bulk edits go through `/bookmarks/bulk`.

### Sleep, resume and connectivity

- **Detect resume** two ways: wall-clock vs monotonic drift above 30 s, and `WM_POWERBROADCAST` on the tray's hidden window.
- **Wait for network:** after resume, probe a configurable URL (default a HEAD to a well-known host) for up to 2 minutes before dispatching.
- **Catch up once:** each overdue bookmark gets one `catchup` check, not one per missed interval. Spread them over min(5 min, count × 1 s).
- **Offline mode:** when the probe fails, pause dispatch and stop counting errors. Resume when it succeeds.
- **Battery:** read `psutil.sensors_battery()` once a minute and apply each bookmark's `on_battery` policy. A global "pause on battery saver" option is off by default.
- **No keep-awake:** the engine never prevents sleep unless the user turns on "keep awake while AutoWatch runs".

## Actions and notifications

When the gate passes, the engine queues the bookmark's actions as durable jobs, so a failed email retries instead of disappearing. v1 ships toast, email and ntfy as alert channels; Pushover and webhooks follow after v1.

| Action             | Parameters                                                   | Behaviour                                                    |
| ------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `toast`            | On by default                                                | Windows toast: bookmark name, 200-char summary of added text, buttons Open and Mark read; coalesced per 30 s window |
| `sound`            | WAV path                                                     | Played with `winsound`                                       |
| `open`             | internal or external                                         | Opens the change view in the UI, or the URL in the default browser |
| `email`            | Recipients; format `full`, `simple` or `changes_only`; attach screenshot; template; priority | SMTP over TLS via aiosmtplib; password in keyring; one email per change |
| `export`           | Path template; format `html`, `html_assets`, `text` or `diff_json` | Atomic write; variables `{id}` `{name}` `{datetime}` `{info1..3}` `{date:<fmt>}` |
| `run_program`      | Executable, argument template, timeout                       | No shell; variables `{url}` `{file_new}` `{file_old}` `{file_changes}` `{info1..3}`; output logged |
| `webhook`          | URL, method, JSON body template                              | Post-v1. Bridges to Home Assistant, n8n, Discord, Slack and similar |
| `pushover`, `ntfy` | Token or topic, priority                                     | ntfy in v1: server URL (ntfy.sh or self-hosted), topic, optional access token. Pushover post-v1 |
| `scrape`           | Named fields (CSS, XPath or regex) and output file           | Appends one record per change to CSV, JSON Lines or XML      |
| `script`           | `check_folder(name)`, `pause_bookmark(minutes)`              | Equivalents of WebSite-Watcher's script action               |
| `mark_read`        | None                                                         | Promotes latest to baseline; always runs last; for email-only workflows |

### Execution rules

- Each action becomes an `action_job` row keyed by (change, action index), so reruns are idempotent.
- Jobs run in configured order. Retries back off at 1, 5 and 30 minutes, then up to 5 attempts total.
- Failed jobs appear in the UI's Problems list with the last error.
- Templates are Jinja2 (sandboxed) with `bookmark`, `change` (stats, summary, added_text, keyword_hits) and file paths in scope.
- **Alert privacy:** each bookmark has `alert_privacy`. `private` alerts say only which bookmark changed and link to the app; `content` alerts include the changes. Signed-in bookmarks default to `private`, and switching one to `content` needs an explicit confirmation.
- **ntfy exposure:** on the public ntfy.sh server, message content passes through a third party and the topic name is the only protection unless access control is set. Prefer a self-hosted server or an access-protected topic.

## Desktop UI

The UI is a PySide6 client of the engine API, laid out like an email client; it never fetches pages itself and can close at any time without affecting checks.

### Main window

- **Left pane, folder tree:** real folders plus virtual folders (saved queries). Built-ins: Changed today, Unread, Errors, Needs login, Keyword hits.
- **Middle pane, bookmark list:** status icon, name, last changed, last checked, next due, interval, error count, keyword hits. Unread rows are bold. Sortable, filterable, multi-select. A lazy model pages rows from the API so 10,000 bookmarks stay fast.
- **Right pane, change viewer:** tabs for Highlighted (in-page), Text diff, New, Old, Screenshot diff and Check log. Highlighted and Text diff show last read → latest by default; a history list steps through each alert's own diff.
- **Toolbar:** Add, Check selected, Check all, AutoWatch start/pause, Mark read, Next unread, Search.
- **Status bar:** engine connection, queue length, checks in flight, browser running, offline and battery state.

### Review shortcuts

| Key        | Action                                           |
| ---------- | ------------------------------------------------ |
| N or Space | Next unread change                               |
| R          | Mark read                                        |
| O          | Open URL in default browser                      |
| F          | Flag false positive (proposes automatic filters) |
| C          | Check selected now                               |
| Ctrl+E     | Edit bookmark                                    |

### Bookmark editor

A tabbed dialog: General (name, URL, folder, method), Schedule, Filters (with live Test filter), Keywords, Gate, Highlight, Actions, Login, Advanced (headers, POST body, user agent, proxy, timeouts, browser options) and Notes (info fields). Bulk edit applies chosen fields to all selected bookmarks.

### Add-bookmark assistant

1. Paste a URL; the engine runs `POST /preview` with method `auto`.
2. Show the rendered preview, the detected type (page, feed, PDF, JavaScript app) and the chosen method.
3. Fetch twice, 5 s apart, and pre-propose ignore filters for anything that already differs (clocks, tokens).
4. Let the user pick a region to watch or keep the whole page, then save.

### Filter assistant

In the Highlighted or New view, the user selects text (Alt+select, or right-click). A QWebChannel bridge reports the selection and its DOM path. The popup offers:

- Ignore this element
- Watch only this element
- Ignore text like this (numbers generalized)
- Ignore from page start to here
- Ignore from here to page end

Each choice shows its Test filter result before saving.

### Other screens

Settings (defaults, network, email, notifications, retention, backup, data folder), Problems (errors and failed actions), Check log, Import/Export, and the Macro recorder.

### Tray (owned by the engine)

- Icon states: normal, unread changes, paused, offline, error.
- Menu: Open PageWatch, Check all now, Pause AutoWatch (1 hour or until resumed), Quit engine.
- Dark mode follows the Windows setting in both tray and UI.

## Engine local API

The engine serves JSON over HTTP and pushes events over one WebSocket, bound to 127.0.0.1 and protected by a token regenerated on every start.

### Discovery

On start the engine writes `engine.lock` in the data folder: `{pid, port, token, version, started_at}`. A named mutex per data folder enforces one engine. The UI and CLI read the lockfile; if no engine is running, the UI offers to start it.

### Endpoints

| Method             | Path                                                         | Purpose                                                      |
| ------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ |
| GET                | `/health`                                                    | Version, uptime, queue length, outcome counts (24 h), RSS, browser state |
| GET, POST          | `/folders`                                                   | List, create                                                 |
| PATCH, DELETE      | `/folders/{id}`                                              | Update, delete                                               |
| GET                | `/bookmarks?folder=&status=&unread=&q=&cursor=`              | Paged list                                                   |
| POST               | `/bookmarks`                                                 | Create; schedules the first check                            |
| GET, PATCH, DELETE | `/bookmarks/{id}`                                            | Read, update, delete                                         |
| POST               | `/bookmarks/bulk`                                            | Update, move, enable or delete many                          |
| POST               | `/bookmarks/{id}/check`                                      | Check now; `?force=true` skips conditional GET               |
| POST               | `/check`                                                     | Check a list of ids, a folder, or all                        |
| GET                | `/bookmarks/{id}/changes`                                    | Change history                                               |
| GET                | `/changes/{id}/render?view=highlight|text|new|old|screenshot` | One change's gate diff for the history view, as sanitized HTML or PNG |
| GET                | `/bookmarks/{id}/diff?view=highlight|text`                   | The viewer's default: everything unread, baseline → latest. Computed in the worker pool, cached per (baseline, latest) pair, and reported as identical when the two match |
| POST               | `/bookmarks/{id}/read`                                       | Mark read (baseline = latest)                                |
| POST               | `/changes/{id}/false-positive`                               | Returns proposed automatic filters                           |
| POST               | `/preview`                                                   | Trial fetch of a URL and options, nothing stored             |
| POST               | `/bookmarks/{id}/test-filter`                                | Pipeline with a candidate config, nothing stored             |
| GET, PUT           | `/settings`                                                  | Global settings                                              |
| POST               | `/autowatch`                                                 | `{state: running|paused, until?}`                            |
| POST               | `/import`; GET `/export`                                     | Bookmark import and export                                   |
| POST               | `/reports/{template}`                                        | Generate a report                                            |
| POST               | `/backup`, `/restore`                                        | Backup and restore                                           |
| WS                 | `/events`                                                    | Push: `check_started`, `check_finished`, `change_detected`, `bookmark_updated`, `engine_state`, `problem` |

All bodies are Pydantic models from `models.py`. The generated OpenAPI schema is committed as `docs/openapi.json`, and the UI uses a typed client built on it.

## Extensibility

Plugins are Python modules in the data folder's `plugins\` directory that implement optional hook functions, mirroring WebSite-Watcher's plugin events; a CLI replaces its scripting language and command line.

### Plugin hooks

| Hook                                             | Runs                    | Can                                                |
| ------------------------------------------------ | ----------------------- | -------------------------------------------------- |
| `before_check(ctx)`                              | Before fetch            | Skip the check; change URL, headers or POST body   |
| `merge_pages(ctx, result)`                       | After fetch             | Fetch and append more pages                        |
| `preprocess(ctx, html) -> str`                   | Before block extraction | Rewrite content                                    |
| `compare(ctx, old, new) -> Verdict | None`       | After diff              | Force change, force no change, or defer            |
| `check_keywords(ctx, added_text) -> bool | None` | In the gate             | Custom keyword logic; OR-ed with built-in keywords |
| `on_change(ctx, change)`                         | After gate pass         | Custom action                                      |
| `after_check(ctx, run)`                          | After every check       | Logging, metrics                                   |

`ctx` exposes the bookmark (read-only, plus `set_property`), a rate-limited `http_get`, `log()`, a per-bookmark persistent `state` dict, and the regex and HTML helpers the pipeline uses.

- Plugins are trusted local code, loaded only from the plugins folder and assigned per bookmark.
- A hook that raises is logged and treated as absent; the check continues.
- A hook slower than 5 s logs a warning in the Check log.
- Shipped examples: price-range alert, alert when a page has not changed for N days, alert only when more than one new item appears, extract PDF links.

### CLI

`pagewatch-cli` talks to the running engine's API:

- `add`, `list`, `check`, `pause`, `resume`, `read`
- `report <template> --out <file>`
- `import <file>`, `export <file>`
- `backup`, `restore <zip>`
- `service install|uninstall` (Task Scheduler registration)

Anything WebSite-Watcher scripts do on a timer becomes a Task Scheduler entry calling the CLI.

## Import, export, reports and backup

Everything PageWatch knows lives in one data folder, so import, export, backup and moving to another PC are all file operations.

| Feature                   | Formats and behaviour                                        |
| ------------------------- | ------------------------------------------------------------ |
| Import                    | Browser bookmarks HTML (Netscape format), plain URL list, CSV or XLSX with columns `name, url, folder, interval, keywords`; dedupe by normalized URL |
| Export                    | Full config as versioned JSON; OPML for feed bookmarks; CSV list |
| Reports                   | Jinja2 templates to HTML, CSV, JSON or XML. Built-ins: changed since last report, all bookmarks with status, errors. Run on demand, from the CLI, or as an action |
| Automatic backup          | Daily at 03:00 local, or at the next wake: SQLite online backup + settings, macros and plugins into a zip in `backups\`; keep 14. Blobs optional (off by default) |
| Manual backup and restore | From Settings or the CLI; the engine restarts itself after a restore |
| Portable mode             | `--data-dir <path>` on engine, UI and CLI; a second engine may run against a different data folder |
| Moving PCs                | Backup zip with blobs, restore on the new machine. Secrets are not exported; the UI lists which credentials to re-enter |

## Continuous operation and reliability

The engine is built to stay up for weeks: it starts at logon, restarts after crashes, shrinks when idle, and commits every check atomically.

### Autostart and lifecycle

- `pagewatch-cli service install` registers a Task Scheduler task: trigger at the user's logon; restart every 1 minute on failure; no run-time limit; allowed on battery; never start a second instance.
- A named mutex per data folder prevents duplicate engines.
- Graceful shutdown on Quit, Ctrl+C or `WM_QUERYENDSESSION`: stop dispatch, wait up to 10 s for in-flight checks, close the browser, checkpoint the WAL.
- Exit code 3 means "restart me"; Task Scheduler brings the engine back.

### Crash safety

- SQLite in WAL mode with `synchronous=NORMAL`. Each check's writes are one transaction.
- Blobs are written atomically before any row references them. Orphans are collected nightly.
- On startup, any `check_run` without `finished_at` is closed as `error: interrupted`, and its bookmark is re-queued.
- Action jobs survive restarts because they live in `action_job`.

### Resource limits

| Resource             | Limit         | When exceeded                               |
| -------------------- | ------------- | ------------------------------------------- |
| Idle browser         | 10 minutes    | Close the browser                           |
| Browser pages served | 500           | Recycle the browser                         |
| HTTP body            | 20 MB         | Error `too_large`                           |
| Text for one diff    | 20,000 blocks | Line-level diff, `diff_degraded`            |
| Engine RSS           | 1.5 GB        | Recycle browser; if still high, exit code 3 |
| Event-loop lag       | 10 s          | Log a stack dump                            |

### Observability

- structlog JSON logs in `logs\engine.log`, rotating 10 MB × 5. One line per check with bookmark id, method, duration and outcome.
- Secrets, cookies and Authorization headers are redacted.
- The DEBUG level can be toggled in Settings without a restart.
- Hourly RSS, CPU and queue-length samples go into the `metric` table and feed `/health`.

### Updates

v1 updates are manual: install the new build over the old one. Migrations run at startup after an automatic backup.

## Security and privacy

The engine is reachable only from this user's session, never stores a password in plain text, and never runs a monitored site's JavaScript inside the viewer.

- **Local API:** binds 127.0.0.1 on a random port. Every request needs `Authorization: Bearer <token>`; the WebSocket sends the token in its first message. Requests carrying a browser `Origin` header are rejected, which blocks drive-by calls from web pages.
- **Lockfile:** lives under `%LOCALAPPDATA%`, which only this user can read.
- **Secrets:** keyring (Windows Credential Manager). The database stores key names only. Exports and backups never include secrets.
- **Viewer safety:** rendered change views have scripts and event handlers stripped (nh3). Remote images and styles load only when the user turns them on.
- **Login profiles:** persistent browser profiles stay in the data folder; each bookmark has a "wipe session" button.
- **run_program:** argument list, no shell, template values escaped. Enabling it on a bookmark needs an explicit confirmation.
- **Plugins and templates:** plugins are trusted local code; templates render in Jinja2's sandbox.
- **TLS:** verification is on; a per-bookmark opt-out shows a warning badge.
- **Telemetry:** none.

## Project layout, tooling and testing

One repository, one package with three entry points, and a test suite that drives the real engine against a local fixture web server.

```text
pagewatch/
  pyproject.toml                 # uv; ruff, mypy, pytest config
  src/pagewatch/
    models.py                    # Pydantic models shared by engine, UI, CLI
    engine/
      main.py                    # entry point: pagewatch-engine
      clock.py                   # injectable Clock; no direct datetime.now()
      scheduler.py  runner.py  power.py  tray.py  plugins.py
      fetch/      static.py browser.py screenshot.py documents.py feed.py ftp.py localfile.py macro.py
      pipeline/   extract.py filters.py special.py autofilter.py diff.py render.py gate.py keywords.py
      actions/    toast.py email.py export.py program.py webhook.py push.py scrape.py script.py
      store/      db.py blobs.py retention.py backup.py migrations/0001_init.sql
      api/        app.py events.py routes_*.py
    ui/
      main.py                    # entry point: pagewatch-ui
      client.py                  # typed API client
      windows/    main_window.py bookmark_editor.py filter_assistant.py settings.py problems.py
      web/        diff.css bridge.js
    cli/main.py                  # entry point: pagewatch-cli
    data/         volatile_patterns.yaml cookie_banner_selectors.txt
  tests/
    unit/  integration/  soak/
    fixtures/sites/              # paired old/new pages with expected outcomes
  docs/       ARCHITECTURE.md  DECISIONS.md  openapi.json
  packaging/  pagewatch.spec  installer.iss
```

### Tooling

Python 3.12 via uv; ruff for lint and format; mypy `--strict` on `engine/` and `models.py`; pytest with pytest-asyncio; respx for HTTP mocks; an aiohttp fixture server with controllable page changes; Playwright tests marked `browser`.

### Test strategy

| Layer         | Covers                                                       |
| ------------- | ------------------------------------------------------------ |
| Unit          | Block extraction, every filter type, special filters, both diff stages, keyword parser (table-driven), schedule math including DST, gate order |
| Golden corpus | ≥ 30 page pairs with expected outcome and highlighted-output snapshot: news timestamp, price drop, reordered list, cookie banner, JS-only app, PDF revision, new RSS item, table cell update, empty-page glitch, blacklisted error page, full-page rewrite, product-card price drop, stock flip-back, records feed with a new lottery |
| Integration   | Full engine against the fixture server: check cycles, actions, simulated sleep (clock jump), offline mode, kill -9 mid-check then restart |
| Soak          | 24 h, 1,000 fixture bookmarks at 1–60 min intervals, plus a 2 h capacity run at 10,000: RSS within budget, no stuck checks, no orphan blobs |

### Working agreement for the coding agent

- Build one milestone at a time; start the next only when the current acceptance criteria pass.
- End every session with: ARCHITECTURE.md and DECISIONS.md updated, full test suite green, and a git commit with a descriptive message.
- Log any deviation from this spec in DECISIONS.md with the reason.
- Ask the owner before adding a dependency not named here, changing a public API shape, or altering the schema outside a migration.

## Milestones

Nine milestones, each shippable on its own; M1 alone already gives a working headless monitor with toasts, and M5 makes it safe to leave running.

### M0 · Skeleton

Repo, uv, `models.py`, database and migrations, blob store, `Clock`, logging, SQLite writer thread, worker-process pool, engine entry point with `/health`, lockfile and single-instance mutex.

- `pagewatch-engine` starts and `/health` answers only with the token
- A second engine on the same data folder exits with a clear message
- `pytest` runs green on a clean checkout

### M1 · Core loop (static pages)

Bookmark and folder CRUD via API and CLI, static fetcher, block extraction, special filters, raw and filtered hash shortcuts, two-stage diff behind the Differ interface, versions with all three pointers, scheduler (interval, times, adaptive), `check_run` log, toast action, mark read.

- 20 fixture bookmarks added via CLI; each fixture change produces exactly one toast
- Unchanged fixtures produce zero toasts over 1 hour
- Restarting the engine preserves every `next_due_at`
- The Differ benchmark on the golden corpus picks an implementation, and a full-page rewrite stays within the 2 s budget
- A stored but unalerted change is not re-diffed and does not reset the adaptive interval on later checks

### M2 · Noise control

Cosmetic, watch and ignore filters (selector, between, text, number_mask), the full gate with cumulative and per-check thresholds, keyword parser with page(), same-block and proximity scopes, test-filter endpoint, automatic filters.

- Every golden-corpus pair yields its expected outcome
- A false-positive flag on the timestamp fixture produces a filter that stops the alert on the next check
- A price drop on the product-card fixture fires a `page()` + `num()` rule
- "in stock" returning after "out of stock" alerts with no special option
- Four small additions fire one cumulative alert labelled with its check count, whether checked hourly or once after a simulated sleep

### M3 · Desktop UI

Main window, change viewer (highlighted, text, new, old), bookmark editor, add assistant, tray, review shortcuts.

- A full review pass is possible keyboard-only
- Closing and reopening the UI never interrupts checks
- The bookmark list stays responsive with 10,000 bookmarks

### M4 · Dynamic pages and other sources

Browser fetcher, auto method detection, screenshot method and pixel diff, PDF, DOCX, XLSX, feeds, local files, FTP, records sources (JSON and CSV).

- The JS-only fixture is detected and switched to browser automatically
- Screenshot diff boxes the changed region on the visual fixture
- The PDF revision fixture highlights the changed paragraph
- The browser closes after 10 idle minutes, and browser checks fall back to bundled Chromium when Edge won't launch
- A new lottery in the records fixture produces exactly one alert naming it, and Housing Connect's live listings render through the browser fetcher (manual check)

### M5 · Unattended operation

Resume detection, connectivity probe, catch-up, battery policy, retries, error notifications, durable action queue, retention and GC, automatic backup, RSS guard, `service install`.

- A simulated 3-hour sleep yields one staggered catch-up check per overdue bookmark
- 10 minutes offline adds zero to any error counter
- kill -9 mid-check, then restart: no corrupt rows, interrupted check re-queued
- 24-hour soak stays within the resource budgets

### M6 · Actions and logins

Email, ntfy, export, run program, scrape and script actions; optional semi-attended sign-in and the portal experiment; Check-Macro recorder and replay; cookie import; proxies; per-host overrides. Pushover and webhooks come after v1.

- A macro logs into the fixture login site and detects a change behind it
- The portal experiment is run and its findings (idle timeout, any code or CAPTCHA on routine logins, challenge pages) are recorded in DECISIONS.md before any stored-password login is enabled
- A semi-attended sign-in on the fixture site checks immediately; an expired session yields exactly one needs-login alert and pauses the bookmark
- The ACCESS HRA E-Notices bookmark never requests a notice PDF, per the request log (manual check)
- A failing SMTP server leads to retries, then a Problems entry
- A changes-only email renders correctly in Gmail and Outlook.com (manual check)
- An ntfy alert reaches the phone, and private bookmarks send no page content

### M7 · Power features

Filter assistant in the UI, Follow-Links, merge pages, virtual folders, import and export, reports, plugins with shipped examples, complete CLI.

- Follow-Links on the fixture site creates child bookmarks within its depth and page limits
- Each shipped plugin example passes its integration test

### M8 · Packaging

PyInstaller build, Inno Setup installer, upgrade path with pre-migration backup, short user guide.

- A clean Windows VM installs, autostarts at logon and survives an upgrade with data intact

## Appendix: feature traceability

PageWatch v1 covers WebSite-Watcher's monitoring core; it drops cloud sync, the phone app and social-media conversion, and adds ntfy alerts, records sources, page-context keywords and NOT keywords.

| WebSite-Watcher feature                                      | In PageWatch v1                           | Section                            | Milestone |
| ------------------------------------------------------------ | ----------------------------------------- | ---------------------------------- | --------- |
| Web pages (HTTP, HTTPS)                                      | Yes                                       | Fetch layer                        | M1        |
| Highlight changes, Standard method                           | Yes                                       | Change detection                   | M1        |
| AutoWatch intervals and time settings                        | Yes                                       | Scheduler                          | M1        |
| Automatic (adaptive) interval                                | Yes                                       | Scheduler                          | M1        |
| Special filters                                              | Yes                                       | Normalization and filtering        | M1–M2     |
| Ignore and watch filters, wildcards, regex                   | Yes                                       | Normalization and filtering        | M2        |
| Automatic filters                                            | Yes                                       | Normalization and filtering        | M2        |
| Cosmetic filters, cookie-banner removal                      | Yes                                       | Normalization and filtering        | M2        |
| Keyword alerts (AND, whole word, regex, price compare)       | Yes, plus NOT, proximity and page context | Alert gating                       | M2        |
| Ignore updates (blacklist, whitelist, minimum characters)    | Yes                                       | Alert gating                       | M2        |
| Exact and Table highlight methods                            | Yes                                       | Change detection                   | M2        |
| JavaScript pages via browser engine                          | Yes                                       | Fetch layer                        | M4        |
| Screenshot method and screenshot filters                     | Yes                                       | Fetch layer; Change detection      | M4        |
| PDF, Word, Excel                                             | Yes                                       | Fetch layer                        | M4        |
| RSS and Atom feeds                                           | Yes                                       | Fetch layer                        | M4        |
| FTP and FTPS                                                 | Yes                                       | Fetch layer                        | M4        |
| Local files and folders                                      | Yes                                       | Fetch layer                        | M4        |
| Error notification after N errors, retry once                | Yes                                       | Alert gating                       | M5        |
| AutoBackup, backup and restore                               | Yes                                       | Import, export, reports and backup | M5        |
| Actions: sound, open, email, export, run program, mark read, script | Yes                                       | Actions                            | M1, M6    |
| Scraper (Business edition)                                   | Yes                                       | Actions                            | M6        |
| Check-Macros and web logins, plus optional semi-attended sign-in | Yes                                       | Fetch layer                        | M6        |
| Proxy                                                        | Yes                                       | Fetch layer                        | M6        |
| Proxy rotation                                               | No                                        | Not in v1                          | —         |
| Local Website Archive export                                 | Partial: export action with assets        | Actions                            | M6        |
| Filter-Assistant                                             | Yes                                       | Desktop UI                         | M7        |
| Follow-Links (whole-site monitoring)                         | Yes                                       | Fetch layer                        | M7        |
| Merge pages                                                  | Yes                                       | Fetch layer                        | M7        |
| Virtual folders                                              | Yes                                       | Desktop UI                         | M7        |
| Plugins                                                      | Yes, in Python                            | Extensibility                      | M7        |
| Scripting language and command line                          | Replaced by CLI + API                     | Extensibility                      | M7        |
| Reports (HTML, CSV, JSON, XML)                               | Yes                                       | Import, export, reports and backup | M7        |
| Import and export                                            | Yes                                       | Import, export, reports and backup | M7        |
| Portable install, multiple instances                         | Yes, via `--data-dir`                     | Import, export, reports and backup | M8        |
| Lightweight mode for 24/7 use                                | Built in: engine has no browser UI        | Key decisions                      | M0        |
| Facebook, X, Instagram as feeds                              | No                                        | Non-goals                          | —         |
| Cloud Sync                                                   | No                                        | Non-goals                          | —         |
| Smartphone app                                               | No; ntfy instead                          | Actions                            | —         |
| Password-protected bookmark files                            | No; relies on the Windows account         | Security and privacy               | —         |
| ntfy action (webhook and Pushover after v1)                  | New                                       | Actions                            | M6        |
| Records sources (JSON and CSV, keyed by ID)                  | New                                       | Fetch layer                        | M4        |

## Sources

- [WebSite-Watcher features](https://www.aignes.com/features.htm)
- [Compare editions](https://www.aignes.com/editions.htm)
- [News and updates (v26.4)](https://www.aignes.com/news.htm)
- [Online help: contents](https://www.aignes.com/help/wsw/index.htm)
- [Help: bookmark check technology](https://www.aignes.com/help/wsw/UI_BOOKMARK_Check.html)
- [Help: actions](https://www.aignes.com/help/wsw/UI_BOOKMARK_Actions.html)
- [Help: browser engines](https://www.aignes.com/help/wsw/Manual_Browser_Engine.html)
- [Help: AutoWatch and check options](https://www.aignes.com/help/wsw/UI_OPTIONS_CHECK.html)
- [Help: keywords](https://www.aignes.com/help/wsw/UI_BOOKMARK_Keywords.html)
- [Help: ignore updates](https://www.aignes.com/help/wsw/UI_BOOKMARK_IgnoreUpdates.html)
- [Help: highlight methods](https://www.aignes.com/help/wsw/Highlight_Methods.html)
- [Help: filter overview](https://www.aignes.com/help/wsw/Filter_Overview.html)
- [Help: special filters](https://www.aignes.com/help/wsw/Filter_SpecialFilter.html)