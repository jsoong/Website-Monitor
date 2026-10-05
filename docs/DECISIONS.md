# Decisions and deviations from the spec

Every place the implementation departs from, or fills a gap in, the technical specification
is logged here with its reason. Newest entries at the bottom of each milestone section.

## M0 · Skeleton

- **`pyyaml` added.** Not on the spec's dependency list, but the spec mandates
  `data/volatile_patterns.yaml`, which needs a YAML parser. Added at M0 so the dependency set
  is stable. (`cdifflib`, `fast-diff-match-patch`, `rapidfuzz` are on the list as Differ candidates.)
- **`data/` lives at `src/pagewatch/data/`**, not the repo root, so it ships as package data in
  the wheel and the PyInstaller build.
- **Engine sub-packages.** `fetch/ pipeline/ actions/ store/ api/` live under
  `src/pagewatch/engine/` as in the spec's component table. `ui/` and `cli/` are siblings of `engine/`.
- **Composition root is `engine/core.py` (`Engine`)**, plus `engine/settings.py` and
  `engine/workers.py`. The spec's layout lists `main.py` as the entry point only; keeping
  wiring out of it lets tests build an `Engine` without argv parsing, uvicorn, or signals.
- **Single-instance guard.** Windows: a named kernel mutex (`Local\PageWatch-<hash of folder>`)
  via `ctypes`, as specified. Elsewhere (development/CI): an `flock` on `engine.mutex`. The
  Windows branch is exercised only on Windows; a manual check is listed under M8.
- **Exit codes.** 0 ok, 1 error, **2 another engine already owns the data folder**, 3 restart me.
  Task Scheduler is configured with "do not start a new instance", so a duplicate launch from
  the task never reaches the exit-2 path; a duplicate started by hand gets a clear message.
- **uvicorn signal handling disabled** (`capture_signals` overridden). uvicorn would otherwise
  install its own handlers and re-raise SIGINT/SIGTERM after serving; the engine owns shutdown
  (Quit, Ctrl+C, SIGBREAK, WM_QUERYENDSESSION).
- **WebSocket backend** `websockets-sansio` (the maintained uvicorn implementation).
- **Process pool uses the `spawn` start method on every OS** so Linux development behaves like
  Windows (no inherited threads, locks or SQLite handles), with `max_tasks_per_child=500`
  to cap worker memory growth.
- **Extra indexes in migration 0001** beyond the spec's schema: `ix_bookmark_folder`,
  `ix_bookmark_url` (import de-duplication), `ix_change_*`, `ix_check_run_*`,
  `ix_action_job_status`, `ix_metric_name_ts`. Purely additive; no column differs from the spec.
- **`bookmark.*_json` columns store sparse overrides.** "Folders carry defaults; a bookmark
  overrides any field" requires knowing which fields a bookmark set itself, so the columns hold
  only what the user set and the effective config is
  `Settings.default_* <- folder chain defaults_json <- bookmark JSON`. The API returns both the
  effective models and the stored `overrides`. `PATCH` merges into the stored overrides and a
  `null` removes an override (back to inherited).
- **`actions_json` accepts a bare list or an object.** The spec's column default is `'[]'` but
  also says the column carries `alert_privacy`; the model accepts `[...]` (actions only) and
  `{"actions": [...], "alert_privacy": ...}`. The API writes the object form, and puts a `toast`
  action in new bookmarks ("toast: on by default").
- **`ignore_removed` lives only in `GateConfig`.** The spec lists it both under special filters
  and as gate rule 5; it acts at the gate, so there is one field, shown on the Filters tab.
- **Redaction** covers keys (password, token, authorization, cookie, secret, ...) and
  `Bearer`/`Basic`/`Digest` values and `Authorization:`/`Cookie:` text inside strings.
- **Windows `time.monotonic()` may include suspended time** on some builds, which would hide a
  sleep from the wall-vs-monotonic drift check. M5 therefore also watches heartbeat lateness
  (a timer that fires hours late) in addition to `WM_POWERBROADCAST`.

## M1 · Core loop

### Differ: `rapidfuzz`

Differ: `rapidfuzz` (``rapidfuzz.distance.Indel.opcodes``, bit-parallel LCS in C++). Chosen by
`tools/bench_differ.py` (medians of 5; "opcodes" is the raw differ call, "full" is the whole
two-stage `diff_blocks` including the word diff and stats; `*` = a bound was hit and
`degraded` set; `(n)` = edit-script size, identical across differs wherever all finished):

```
scenario                                                               difflib                  cdifflib                 rapidfuzz                       dmp
                                                       opcodes / full (script)   opcodes / full (script)   opcodes / full (script)   opcodes / full (script)
typical: 800 blocks, 3 edits                             0.6ms /     0.5ms (    3)     0.4ms /     0.4ms (    3)     0.0ms /     0.2ms (    3)     0.2ms /     0.4ms (    3)
large: 5,000 blocks, 25 edits                            6.0ms /     8.4ms (   34)     3.9ms /     5.9ms (   34)     6.1ms /     8.0ms (   34)     1.5ms /     3.8ms (   34)
reorder: 5,000 blocks, 500-block section moved           3.0ms /     3.0ms ( 1000)     2.0ms /     2.6ms ( 1000)     1.8ms /     3.4ms ( 1000)     2.2ms /     3.3ms ( 1000)
duplicates: 5,000 blocks from 40 distinct              241.8ms /   230.0ms (   57)   165.4ms /   155.3ms (   57)     1.9ms /     3.8ms (   57)     0.8ms /     2.8ms (   57)
rewrite: 5,000 blocks all different                      1.9ms /    30.9ms*(10000)     1.4ms /    28.4ms*(10000)     7.0ms /    34.4ms*(10000)    94.8ms /   125.4ms*(10000)
rewrite: 20,000 blocks all different                     7.8ms /   118.7ms*(40000)     6.0ms /   113.6ms*(40000)   140.7ms /   253.8ms*(40000)  1439.0ms /  1520.2ms*(40000)
half rewrite: 10,000 blocks, 50% replaced             5850.5ms /  6485.2ms*(10000)  1745.7ms /  2293.3ms*(10000)    37.2ms /   238.8ms (10000)   271.3ms /   499.8ms (10000)

* = diff_blocks degraded (hit a bound).  (n) = size of the edit script.
```

* **difflib and cdifflib are unsafe as the stage-1 differ**: on a 10,000-block page with half the
  blocks replaced they take 5.9 s and 1.7 s *inside a single call*, which the 2 s time budget
  (checked between replace runs) cannot interrupt; on many-duplicate pages they are ~100x slower.
* **fast-diff-match-patch** is good on typical pages but slow on full rewrites (1.4 s at 20,000
  blocks) because blocks must be mapped to characters first.
* **rapidfuzz** stays within 3-250 ms on every scenario, including a full-page rewrite at the
  20,000-block cap (254 ms, well inside the 2 s budget). A unit test pins that bound.
* Two extra bounds the benchmark showed were needed, both in `pipeline/diff.py`: (1) a replace run
  is rejected on a cheap lower bound (`spaces + 1` per block) *before* tokenizing, since tokenizing
  700k tokens just to refuse them cost ~800 ms; (2) `MAX_CELLS` (6e8) caps `len(old) * len(new)` so
  Indel's bit matrix cannot allocate gigabytes on a pathological 100k-block page (the middle then
  becomes one block-level replace run, `degraded`).
* The implementation is selectable with `PAGEWATCH_DIFFER` for comparison; `DEFAULT_DIFFER_NAME`
  lives in `pipeline/differs.py`.

### Diff semantics

* **Tokens follow UAX #29 word boundaries closely enough to matter:** `1,099`, `3.5.2` and `don't`
  are single tokens; every other punctuation mark is its own token; trailing whitespace is attached
  to the token (so `"".join` of the tokens reproduces the text, as in the spec's `"Price "`).
  Consequence: the spec's illustrative `["del","$19"],["ins","$17"]` is, strictly, `eq "$"` then
  `del "19"` / `ins "17"`, because the `$` is common to both sides.
* **`stats.changed_words` added** (not in the spec's example). "Changed words" for thresholds means
  `max(removed, added)` per change hunk, so replacing one word is one changed word, not two; pure
  insertions and deletions count in full. `added_words`/`removed_words` keep their literal meaning.
* **"Changed text" for keywords (M2) is block-aware.** `DiffResult.changed_regions` returns whole
  inserted blocks and, for edited blocks, the changed spans *widened to whole whitespace-delimited
  words*. Without this an edit `$1,299 -> $1,099` would surface only as `1,099` and the spec's
  `num(\$([\d,.]+)) < 1200` price rule could never match.
* **`mov` ops sit at the new position** and the old position emits nothing; moves are neither
  counted in stats nor shown as changes in Standard mode, and are changes in Exact mode.
* **Oversized runs set `degraded`** as well as a time-budget overrun (the spec only names the
  latter): the output is block-level either way and the UI should say so.

### Extraction

* **Content after `</html>` is kept.** libxml2 silently discards anything after the closing tag,
  which for a monitor would hide real changes (some CMSes append sections there). `</body>` and
  `</html>` are removed before parsing, which is what browsers effectively do.
* **`<option>`/`<select>` are block elements** so adjacent options never fuse into one word when
  "ignore dropdown entries" is turned off.
* **Special filters run in two places.** NFKC/invisible characters/whitespace and `<option>`
  removal happen during extraction (idempotent text maps, equivalent to applying them last);
  sort, link/image URL blocks and case folding happen in `special.py`. Case folding is a
  *comparison key*, not a rewrite: stored blocks keep their original case so the viewer shows the
  page as written.
* **Raw-HTML mode** (`text_only: false`) compares the decoded HTML source line by line.

### Gate

* M1 implements rules 1-5 and 7 (including cumulative and per-check thresholds). Rule 6 (keywords)
  and rule 8 (plugin hooks) arrive with M2 and M7. Rules 2-4 run *before* anything is stored, so a
  rejected fetch writes no blobs and never moves a pointer (unit-tested).
* **A cumulative alert reports the diff from the gate anchor only when a cumulative threshold is
  active** (`min_changed_words > 0`); otherwise it reports latest -> new. `checks_accumulated` is the
  number of stored versions newer than the anchor.
* A **bad fetch is not an error**: it is `suppressed` with a reason and does not touch
  `consecutive_errors`.

### Runtime

* **Per-host politeness lives in the scheduler's dispatch**, not in the runner. Waiting inside a
  check would tie up one of the 32 global slots per rate-limited item (head-of-line blocking); the
  scheduler instead keeps ready queues per host and starts only items whose host is free.
* **The scheduler re-reads the wall clock at least every 30 s** (`MAX_IDLE_WAIT_S`) so clock
  changes and sleep cannot strand it on a stale timer.
* **Transient errors** (timeout, DNS, connection, 5xx/408/425/429) retry once after
  `transient_retry_s` (60) with trigger `retry` and do not count; a second failure counts. The
  retry marker is carried by the scheduler, not persisted: after a restart one extra free retry is
  possible. `Retry-After` backs the whole host off.
* **Action queue is at-least-once** (a job interrupted by a crash runs again). Jobs re-verify they
  are still `queued` before running: an acceptance test found that a stale snapshot in the poll
  loop could otherwise start a finished job again and send duplicate alerts.
* **Toast coalescing**: changes within `toast_coalesce_s` (30) produce one summary toast; a lone
  change gets its own toast with Open / Mark read. The M1 acceptance criterion "each fixture change
  produces exactly one toast" is verified as *each change id appears in exactly one toast*, with
  coalescing disabled for the one-at-a-time test and enabled in a dedicated test.
* **Time zone**: `Settings.timezone` (IANA name) drives `times`/`days`/`window` through `zoneinfo`;
  unset, the OS local zone is used (DST handled by the C library). `tzdata` is added on Windows
  because `zoneinfo` has no system database there (PEP 615). Times in a DST gap move forward and an
  ambiguous time fires once, at its first occurrence.
* **Jitter never goes below the 60 s floor** (it is applied after the floor, then clamped).
* **Randomness is injectable** (`Engine(rng=...)`) so scheduling tests are deterministic.
* **List endpoints return `BookmarkSummary`**, a light row; the full effective config is returned
  by `GET /bookmarks/{id}`. Paging is keyset-based on `(sort key, id)`.
* **CLI** (`pause`/`resume`): without ids they pause/resume AutoWatch; with ids they disable/enable
  those bookmarks.
* **Filter edits re-normalise stored versions.** The raw-hash shortcut would otherwise hide a new
  filter until the page changed, so `PATCH` of `filter`/`highlight_mode`/`source_type` re-runs the
  pipeline over the versions the three pointers reference (`Engine.rebuild_versions`).

## M2 · Noise control

### Keywords

* **A keyword matches the *whole* changed block but only counts if the match overlaps a changed
  span** (`DiffResult.change_set` -> `ChangedBlock(text, spans)`). Found by the golden corpus: the
  spec says "in stock" returning after "out of stock" must alert with no special option, but that
  edit changes only the word *In* (the word *stock* is unchanged context), so matching the phrase
  against the changed words alone could never succeed. Matching against the whole block fixes
  that, and the overlap requirement keeps it precise: `sale` does not fire when only an unrelated
  timestamp in the same paragraph changed, `"in stock"` fires (its match covers the changed word),
  `$1,299 -> $1,099` keeps its `$` for `num(\$([\d,.]+))`. Inserted blocks count in full.
  `DiffResult.changed_regions` (changed spans widened to whole words) is kept for plugins'
  `check_keywords(ctx, added_text)` and for summaries.
* `num(...)` is evaluated for **every match** of the regex in the changes (any satisfying
  comparison fires), not only the first; with a page(...) context term this is what a price rule
  wants. `1,099`, `1.099,50`, `1,5` and `$1,099.00` all parse.
* `-term` (NOT) vetoes only when its match overlaps the change; `-page(term)` vetoes on the whole page.
* `//` lines in a keyword list are comments (not in the spec; costs nothing and helps long lists).
* Keywords are evaluated against changes since the **latest** version even in cumulative mode
  (spec: rule 6 compares "changes since latest"); only the word threshold uses the anchor.
* `change.keyword_hits_json` holds the gating rules that fired *plus* matches of the highlight-only
  list (so the bookmark list can show them); only the former decide the alert.
* Keyword syntax errors are rejected with a line number at the model boundary (HTTP 422), so a
  broken rule never reaches a check.

### Filters

* **Cosmetic and `selector` ignore filters mark elements (`data-pw-skip`) instead of deleting
  them**, so every block keeps its original DOM path (the in-page diff view injects `<ins>/<del>`
  into the original HTML at those paths in M3).
* **`between`**: markers match case-insensitively on the normalised text, exclusive by default
  (`inclusive: true` includes them); every occurrence is a range; open-ended means start/end of
  page; **a marker that is given but not found yields no range**, so an ignore filter can never
  silently swallow the rest of a restructured page. Matching is done with regexes on the original
  text (an earlier version lower-cased and used the offsets, which is wrong for characters such as
  `İ` whose lower-case form has a different length; there is a regression test).
* **Wildcards**: `*` is lazy between literal text (`Updated * ago`) but a *trailing* `*` runs to the
  end of the block (`build *`); `?` is one character.
* **`scope`** (CSS) limits a text/number_mask rule to blocks inside the matched element, and also
  to the block that *contains* a matched inline element (`span.views` inside a `<p>`). A scope
  that matches nothing makes the rule a no-op for that page.
* **`number_mask`** replaces digits with `#` in the scoped blocks, optionally only blocks whose
  text matches `pattern`.
* **Watch** rules are a union in document order; `selector` watch rules cannot apply to sources
  with no DOM (JSON, text) and are ignored there (if no watch rule can apply, the whole source is
  watched). A watched region that vanishes gives an empty page, which is a change.
* **Built-in cookie-banner list** (`data/cookie_banner_selectors.txt`) is compiled once into one CSS
  union and is on by default. In that file a line starting with `# ` is a comment while `#id`
  lines are selectors.
* **Rule validation**: selectors (CSS via `cssselect`, XPath via `lxml`) and scopes must compile,
  cosmetic rules must be selector rules, `number_mask` cannot watch. A rule that still fails at
  run time (stored before a fix) is skipped with a warning, never fails the check.
* **Risk, deferred to M5**: a user-supplied regex can backtrack catastrophically and hang a worker.
  M5 adds a per-job timeout that kills and rebuilds the pool.

### Gate / diff

* **New gate outcome `reorder_only`**: a diff of only moved blocks is not a change in Standard and
  Table mode (spec: moves are not counted), whether or not a threshold is configured. In Exact
  mode moves are not detected, so a reorder is an ordinary change.
* **Exact mode** reuses the stage-1 block alignment as anchors and word-diffs the replaced runs,
  with move detection off. It is *not* a single token diff of the whole page: that costs
  O(tokens²) on pages with 100k tokens, and the two agree wherever blocks line up.
* **Table mode** is Standard plus, on equal-length row runs, `cells` (indices of changed cells per
  row) and `numeric` (whether each row's change is numeric only) on the `rep` op, so the viewer can
  highlight a whole row or just the cell.

### Test filter and automatic filters

* **Test filter**: the candidate config is the *patch* the editor would send to `PATCH
  /bookmarks/{id}` (same merge semantics, `null` removes an override), run over the stored
  baseline -> latest raw blobs in a worker; it reads blobs only and writes nothing (tested by
  counting rows). `from_version_id`/`to_version_id` let it test any two stored versions.
* **False-positive flag**: marks `change.feedback`, then proposes rules per changed block: a text
  regex from `data/volatile_patterns.yaml` when removing that pattern from the old and new text
  makes them identical (scoped to the block when a stable CSS selector exists), otherwise an
  element ignore (CSS, falling back to an absolute XPath). Every proposal is verified by re-running
  the comparison; the response says whether each one alone, and all together, remove the false
  positive, and includes a ready `PATCH` body. Generated-looking ids/classes (`css-1a2b3c9`, hashes)
  are never used in selectors.
* The proposals are never saved by the engine: the user confirms by applying the patch
  (the spec's "saved only if ... the user confirms").

### Golden corpus

* 50 cases in `tests/fixtures/sites/<case>/{case.json, v1.<ext>, v2.<ext>, ..., expected.txt}`:
  per-step expected outcome (`first | unchanged | alert | suppressed | rejected` + reason, keyword
  hits, word counts) and a snapshot of the highlighted text diff of every alert. Regenerate
  snapshots with `UPDATE_GOLDEN=1` and review the diff. Cases that need M4 (PDF, RSS, records,
  JS-rendered pages) are added with M4.
* Writing the corpus before running it caught two defects: the keyword/flip-back problem above and
  a wrong test expectation about word counts.
* The renderer prints a replaced word as `$[-19-]{+17+}` (no space between the marks).

## M3 · Desktop UI

### Engine side (the UI is only a client)

* **Views are rendered in the engine, in the worker pool, as sanitised HTML** (`pipeline/viewer.py`):
  `highlight` (the new version's own HTML with `<ins class="pw-add">` / `<del class="pw-del">`
  injected at each changed block's DOM path), `text` (blocks with inline marks, always works),
  `new` / `old` (stored pages, unmarked). The UI never parses or sanitises page content itself.
* **Highlights are injected at exact character offsets, through inline markup.** The extractor can
  record the raw text nodes each block was built from; changed offsets of the normalised text
  (whitespace collapsed, NFKC) are mapped back through those nodes, so a change inside
  `<a><b>text</b></a>` is wrapped inside the `<b>`. Where mapping is impossible (table rows; a block
  whose text a text filter altered; combining sequences) the block or changed *cell* gets a class
  instead (`pw-ins-block`, `pw-rep-block`, `pw-changed-cell`). Edge whitespace stays outside the mark.
* **Deleted blocks are shown in place** (before the next surviving block, using a `tr`/`li`/`div`
  holder that is valid where it lands) *and* in a side panel; the viewer's "deletions" toggle is a
  class on `<body>` (`pw-del-inline | panel | none`), so toggling needs no round trip.
* **Sanitising**: nh3 allow-list (no script, style, form, iframe, object, svg, event handlers, `javascript:`
  URLs), `rel="noopener noreferrer nofollow"` on links, and a Content-Security-Policy meta
  (`default-src 'none'`). Remote images become `[alt]` placeholders and remote stylesheets are removed
  unless the caller passes `images=true`, which relaxes only `img-src`/`style-src` and adds `<base>`.
  `QWebEngineView` additionally refuses navigation and sends link clicks to the OS browser.
* **The viewer's default diff (`GET /bookmarks/{id}/diff`) is baseline -> latest**, computed in the pool
  and cached as one `view_diff_cache` row per bookmark. The commit that moves either pointer deletes the
  row; the cache write re-checks the pointers so a slow computation cannot store a stale pair. The history
  view (`GET /changes/{id}/render`) uses each change's own gate diff blob. Both return HTML (with
  `X-PageWatch-*` headers) or `format=json`. `view=screenshot` answers 404 until M4 stores screenshots.
* **`POST /preview`** (add assistant) fetches once or up to three times `gap_s` apart on the engine
  clock, builds blocks, classifies the resource (`page | js-app | feed | pdf | json | ...`; a JavaScript
  shell is detected by < 200 readable characters or an empty app mount point) and, with two samples,
  proposes ignore filters for whatever already differs, reusing the false-positive machinery over an
  in-memory blob store. Nothing is persisted; a fetch failure is a 200 with `error` set.
* **List endpoint additions** for the built-in folders: `changed_since`, `keyword_hits`, per-row
  `keyword_hits` (from unread changes), and `GET /bookmarks/counts` (totals, built-ins, per-folder unread).
* **Tray** (`engine/tray.py`): `TrayController` holds all logic (state precedence offline > paused >
  error > unread > normal, tooltips, the six menu actions, refresh on events and every 30 s) and is
  tested here; `PystrayBackend` and the Windows dark-mode registry read are the thin Windows-only parts
  (manual check, M8). Menu callbacks run on the tray's thread and hop onto the engine loop. Icons are
  drawn with Pillow (now a regular dependency: pystray requires it and M4's pixel diff will use it).
* `Engine.drain_background()` waits for fire-and-forget work (re-normalising after a filter edit).

### UI

* **PySide6 is an optional extra** (`uv sync --extra ui`); the engine has no Qt dependency.
* **Threading**: API calls are blocking `httpx` on a `QThreadPool`; `ui/workers.run_async` delivers the
  result *on the GUI thread* through a dispatcher QObject (a plain callable connected to a signal would
  run on the worker thread). Live events use `QWebSocket` (token as the first message, auto-reconnect).
* **The bookmark list is paged by the engine, not by Qt**: `fetchMore` requests the next keyset page
  (200 rows); sort and filter are engine queries; a generation counter drops answers to a superseded
  query. Measured with 10,000 bookmarks against a real engine over HTTP (offscreen Qt, one machine):
  first page 49 ms, worst scroll page 107 ms, sort 41-44 ms, text filter 56 ms, unread/error filters
  43/28 ms, 10,000 `data()` calls 110 ms (spec budget: < 200 ms for scroll, sort and filter).
* **One shared `QWebEngineView`** sits behind the viewer's tab bar (Highlighted, Text diff, New, Old
  are four renderings into it), so a window costs one Chromium view, not four.
* **Keyboard**: N or Space next unread, R mark read, O open URL, F flag false positive, C check
  selected, Ctrl+E edit, Ctrl+N add, Ctrl+Shift+C check all, Ctrl+P AutoWatch, Ctrl+F search,
  Ctrl+1..6 viewer tabs, Delete. Shortcuts are window-wide actions, so they work from any pane, and a
  focused text field keeps its own typing (Qt's ShortcutOverride). "Next unread" pages through the lazy
  list when the next unread row has not been loaded yet, and wraps once.
* **Status bar**: viewer notes ("Nothing unread", "Large change: shown by block") and action feedback
  ("Queued 3 checks", "No more unread changes") have separate labels; they originally shared one and
  the late render of a just-read bookmark overwrote the end-of-review message (found by the
  keyboard-only test).
* **Bookmark editor** edits *effective* values and sends only the difference as a `PATCH`, so untouched
  settings stay inherited; opening and saving an untouched bookmark sends nothing. Bulk edit sends the
  full values of the tabs the user ticks. Filter rules are normalised through `FilterRule` before
  comparing. The tab's **Test filter** posts the candidate to `/test-filter` without saving.
* **Add assistant** defaults to two fetches 5 s apart; verified proposals are pre-ticked, unverified
  ones are not; "watch only this CSS selector" becomes a `watch` rule; a JavaScript app keeps
  `check_method=auto` (the engine switches to the browser in M4).
* **Not in M3** (by the milestone list): the Filter Assistant with Alt+select and the QWebChannel
  bridge (M7), Settings and Problems screens (M5/M6), screenshot diff tab content (M4; the tab shows
  a placeholder), the Login tab (M6).
* **Tests**: `tests/support/engine_thread.py` runs a real engine (fake clock, fixture web server, real
  API) in a background thread so the Qt main thread uses real HTTP and WebSocket. Qt runs with
  `QT_QPA_PLATFORM=offscreen`; QtWebEngine runs headless with `--no-sandbox --disable-gpu` (CI needs
  `libegl1 libnss3 libxkbcommon0 ...`; UI tests skip when PySide6 is not installed).

## M4 · Dynamic pages and other sources

### Scope and dependencies

* **Dependencies added: `playwright`, `pdfminer.six`, `python-docx`, `openpyxl`, `feedparser`,
  `aioftp`**: every one is named in the spec's technology table. Nothing else was added: no numpy
  (the pixel diff is Pillow only), no JSONPath library (a subset is implemented, below), no keyring
  (M6). `tests/` builds its PDF, DOCX, XLSX and PNG fixtures itself (`tests/support/docs.py`).
* **No migration.** `bookmark.fetch_json` carries the new options, `version.screenshot_hash` and
  `check_run.method` already existed, and `change.diff_hash` carries the screenshot diff.
* **Not in M4** (their own milestones): logins, Check-Macros, persistent browser profiles, cookie
  import, per-bookmark proxies and the keyring (M6); Follow-Links and merge pages (M7); resume,
  connectivity, retention and the per-job timeout (M5). The browser manager already has the shape M6
  needs (a context per profile), but only the shared ephemeral contexts exist.

### Fetch layer

* **Dispatch** (`fetch/select.py`): the URL scheme picks the transport (`file:`, `ftp:`/`ftps:`,
  `http(s):`); for web pages `check_method` picks static, browser or screenshot. What the bytes *are*
  (PDF, feed, records) is decided from the content, so `check_run.method` is refined after the fact:
  a static check of a PDF is recorded as `document`, of a feed as `feed`, of records as `records`;
  browser, screenshot, FTP and file checks keep their own name ("how the content was obtained").
  `FetcherSet` is what tests replace with scripted fetchers.
* **Errors without new error kinds.** The spec's `FetchError.kind` list is unchanged. A missing
  local file or FTP path is `http` with status 404, a refused login is 401, a permission error 403
  (`check_run.reason` reads `http_404` and so on); other I/O failures are `connection` (transient).
  Too many directory entries is `too_large` with a message naming `listing.max_entries`, never a
  silently truncated listing.
* **Local files** keep the spec's mtime+size shortcut in the version's `etag` column (`<size>-<mtime
  ns>`), which is how a conditional GET is carried; touching a file without changing it is read, then
  stopped by the raw-hash check. Folders have no cheap signature, so their listing is rebuilt and
  compared by hash.
* **FTP**: explicit FTPS (`AUTH TLS`) for `ftps://` on any port, implicit TLS on port 990. The
  password never lives in SQLite: `fetch.auth.secret_key` names it and an engine-level `SecretStore`
  (an interface only; the keyring-backed store arrives in M6) supplies it, so a missing secret fails
  with "no stored secret named ..." instead of trying an empty password. Not exercised against a real
  FTPS server here (the in-process server has no TLS): listed under manual checks.
* **Feeds**: one `<li>` per entry (title, then summary), de-duplicated by entry id or link. Timestamps
  are left out of the text on purpose (many feeds bump `updated` on every fetch). "Optional enclosure
  download" is `fetch.feed.download_enclosures` with `enclosures_dir`: each enclosure not already
  there is saved (capped per file and 25 per check, never fails the check).
* **Documents** become deterministic HTML (the viewer converts the stored raw bytes again to inject
  highlights at the extractor's DOM paths): PDF text per page with one paragraph per text box and
  no "Page N" headings (inserting a page would otherwise rewrite every later heading); DOCX
  paragraphs and tables in order (headers, footers, footnotes and text boxes are not read); XLSX one
  table per *visible* sheet, capped at 5,000 rows with a warning. Legacy `.doc`/`.xls` are rejected
  with a message saying to save as `.docx`/`.xlsx`. An unreadable document or feed fails the check
  (`parse`); it never becomes an empty page that reads as "everything was removed".
* **Image sources** are treated like binary content plus a one-line description (format, size,
  hash). Pixel comparison of arbitrary image URLs is not in the spec.

### Records sources

* **Config lives in `fetch.records`** (`RecordsConfig`): format (`auto|json|csv`), `path`, `id_field`,
  `filter`, `fields`, `events`, `delimiter`. A `records` bookmark without it is rejected with a 422
  at create/patch (folder defaults may supply it), and again at check time.
* **JSONPath subset**: `$`, `.key`, `['key']`, `[n]` (negative too), `[*]`, `.*`, `..key`. An object
  of objects (`{"123": {...}}`) is read as rows whose key is the ID when a row has none.
* **Row filter language** (spec gives two examples): `field = value`, `!=`, `<`, `<=`, `>`, `>=`,
  `in [a, b]`, `not in [...]`, `contains`, `startswith`, `endswith`, `not`, `and`, `or` (`and` binds
  tighter), dotted field paths, quoted values. Comparison is case-insensitive and numeric when both
  sides parse as numbers (`1,500` counts). Syntax errors are rejected when the config is saved.
* **Blocks are sorted by ID** (numerically when numeric), so a feed that reorders its rows is not a
  change; the block text is `<id_field>: <id> | <field>: <value> | ...` with watched fields in the
  configured order (default: every other field, sorted). A repeated ID keeps the first row; rows
  without an ID are skipped; both warn.
* **A wrong shape fails the check** (`parse`, e.g. "JSONPath '$.data' matched nothing"): an API that
  changed its layout must not look like every record was removed. An empty array that the path does
  find *is* real and is reported as removals.
* **Events** (`new`/`changed`/`removed`) are computed from the block lists by record ID after the
  normal gate. A bookmark that alerts only on some events gets `suppressed` with reason
  `records_events` when the verdict was an alert but none of its events occurred; the version is still
  stored, so the same change is not found again. The alert summary names the records
  ("New: lottery_id: 104 | name: Riverside Commons | ...", at most three per kind).
* **Viewer**: the highlight view falls back to the text diff (records have no DOM); the New and Old
  tabs list the records instead of the raw JSON.

### Browser

* **`BrowserManager`** is built around *generations*: one launched browser plus its contexts. Recycling
  (500 pages, or 3 crashes in a row) retires the current generation and starts a new one on the next
  page, while the retired one finishes its running pages and is then closed. The idle timer runs on
  the engine's injectable clock (30 s granularity), so tests drive it with `FakeClock`.
* **Launch order**: `browser_executable` if set, else the `msedge` channel in new headless mode
  (`--headless=new`), else, or after either fails, Playwright's bundled Chromium, which is downloaded
  with `playwright install chromium` the first time it is missing. If nothing launches the state is
  `unavailable` for five minutes (no install attempt per check) and every browser check fails with a
  `browser` error naming why. New settings: `browser_channel`, `browser_executable`, `browser_args`
  (the sandbox needs `--no-sandbox` as root).
* **The launch is outside the 45 s page budget** (`BrowserManager.ensure()` runs first): a first
  launch that downloads Chromium must not time out as if the page were slow. A hard timeout of
  45 s + 5 s grace still bounds a page that hangs after `load`.
* **At most `min(3, browser_pool)` pages** at once even if the pool setting is raised; the scheduler's
  browser pool is the second gate (it also covers the pool accounting of auto-detected bookmarks).
* **Resource blocking** is per page (images, media and fonts), off for the screenshot method.
  Same-origin iframes (and `about:` frames) are replaced by a `div[data-pw-iframe]` holding their
  body, deepest first; cross-origin frames stay out.
* **Per-bookmark proxy and "verify TLS" opt-out**: TLS opt-out gets its own shared context; a
  per-bookmark proxy is not applied to browser checks until M6 (the global proxy is).

### Method auto-detection

* **Runs on the first check of an `auto` web bookmark only**, and persists `browser` when it switches
  (an `auto` bookmark that is fine statically stays `auto`). Re-detecting on every check would send
  a legitimately short page ("No openings") through the browser each time, and a redesign that empties
  a static page is a change to report, not a reason to switch methods silently.
* **The < 200 characters trigger needs a script that could be the source**: an empty app mount point
  (`app_shell`), a "requires JavaScript" notice, an external script, or at least 2 KB of inline
  script (`little_text:<n>`). A short page whose only script is a few bytes of inline code stays
  static. This tightens M3's rule (which already required a script) because test fixtures with an
  inline build id showed a plain status page being sent to the browser; the browser costs one of the
  three scarce pool slots. Detection looks at the *unfiltered* text, so a watch filter cannot make a
  normal page look empty.
* **How it is recorded**: the first `check_run` has `reason = auto_browser:<why>` and `method =
  browser`, the log has a `method_switched` line, `bookmark.check_method` becomes `browser` in the same
  transaction as the version, and the scheduler moves the bookmark to the browser pool.
* **If no browser works** the first check fails loudly (`browser` error, one problem toast after the
  error threshold) and detection runs again next time. Storing the empty shell instead would
  monitor nothing and say it was fine.
* **`POST /preview` under `auto`** does the same re-fetch, so the assistant shows the rendered page and
  `method: browser`; if the browser cannot be used it keeps the static page and adds a warning.

### Screenshot method and comparison

* **The picture decides.** For `check_method=screenshot` a change is a visual change over the
  threshold; the text rules (keywords, word thresholds, ignore-removed) are not consulted, the
  bad-fetch rules (error status, minimum characters, blacklist, whitelist) still are, and the page text
  is stored anyway so the Text diff, New and Old tabs keep working. Identical pixels are `unchanged`
  whatever the markup did; a difference below the threshold is `unchanged` too and **stores nothing**
  (drift accumulates against the last stored picture, so a slow real change still gets through).
  A bookmark switched to screenshots from another method stores a silent baseline
  (`screenshot_baseline`), since there is nothing to compare with.
* **Pixel diff**: constants from the spec (threshold 24/255, `min_ratio` 0.2 %, height 5 %).
  Different sizes are compared on a common canvas padded with white. Grouping runs on an 8 px grid
  (the mask reduced by 8, dilated by one cell, 8-connected components), which keeps a tall page
  fast; a region's box covers the changed cells, not the dilation halo. At most 50 regions are
  reported (largest first; the total is kept), and a change touching more than 60,000 grid cells is
  one region (grouping a rewrite of a 12,000 px page cost seconds and said nothing).
* **Configuration**: capture options live in `fetch.browser` (`full_page`, `clip`, delays); the
  comparison parameters are filters and live in `filter.screenshot` (`ignore` rectangles, `min_ratio`,
  `height_change_pct`), which is where the spec puts "screenshot filters".
* **Storage**: the PNG is a blob (`version.screenshot_hash`); the gate diff stored in `diff_hash` is
  JSON with `type: "screenshot"` (old/new hashes, ratio, regions, overlay blob). The text views
  ignore such a diff and compute the text diff from the stored blocks.
* **API**: `view=screenshot` on `GET /changes/{id}/render` (that change's own diff) and on
  `GET /bookmarks/{id}/diff` (last read -> latest, computed on demand; it is cheap, so it is not
  cached in `view_diff_cache`). `format=png` returns the overlay with `X-PageWatch-Regions`,
  `-Changed-Pixels` and `-Identical` headers; the default HTML wraps it in the sanitised document
  as a data URI; `format=json` carries `stats`. 404 when no screenshot was stored, 422 for
  `format=png` with another view.

### Alert gating, pipeline and API

* `process_check` resolves the source kind first (`pipeline/sources.py::resolve_kind`: explicit
  `source_type`, then content type, then URL extension, then magic bytes) and returns it, so the
  runner can name the method and the viewer can convert the same bytes the same way.
  `PipelineJob`/`RenderJob`/`RebuildJob`/`TestFilterJob`/`ProposeJob`/`PreviewJob` carry a small
  `source_cfg` (records layout and feed options; nothing secret).
* The false-positive proposer re-parses the *converted* HTML of documents and feeds, so
  "ignore this element" works on a PDF paragraph as it does on a web page.
* **CLI** gained `add --type <source type>` and `add --fetch '<json>'` (otherwise a records bookmark
  could not be created headlessly). `GET /health` now reports `browser_state`.
* **OpenAPI** regenerated: new option models, `format=png`, `view=screenshot`.

### UI

* **Screenshot diff tab** replaces the placeholder: the overlay fitted to the pane width (never
  scaled up) under a caption ("2 changed regions (boxed in red), 41,200 pixels differ" / "Nothing
  unread: ..."), with the history list selecting an alert's own diff exactly like the other tabs, and
  an explanation when the bookmark has no screenshots.
* **Stale responses**: every viewer render request now carries a sequence number and only the newest
  may draw. Before, a late answer to an earlier request for the same tab (for example the refresh an
  engine event triggers) could overwrite the one just asked for; it was found by the new tab's test
  timing out once in a while, and it affected every tab.
* **Editor**: the Advanced tab gained browser options (wait after load, scroll times, mouse moves,
  full-page screenshots) and a records-source JSON box; the Filters tab gained the screenshot
  comparison (changed-pixel threshold, height threshold, ignore rectangles as `x y w h` lines). They
  round-trip without inventing changes. Feed and listing options have no editor fields yet (API and CLI).

### Tests

* **446 -> 791 default tests** (1 skipped: the permission test needs a non-root user), plus **12
  real-browser tests** (`-m browser`). New: unit tests for records, documents,
  feeds, sources/dispatch/models, the pixel diff and its pipeline flow, the browser manager (fake
  browser, fake clock), the browser fetcher (scripted pages), local files and FTP (in-process
  server); 20 new golden cases (PDF, DOCX, XLSX, RSS, records, rendered JS page, folder listing); two
  integration modules (content sources; browser flows with scripted fetchers); UI tests for the
  tab and the editor; a real worker-process round trip of the new jobs; and 12 tests against a real
  Chromium.
* **Real-browser tests are opt-in** (`pytest -m browser tests/browser`): they use
  `/opt/pw-browsers/chromium` or `PAGEWATCH_TEST_CHROMIUM`, with `--no-sandbox`. The sandbox has no
  Microsoft Edge, which makes "Edge will not launch, fall back to the bundled Chromium" a real failure
  rather than a simulated one (the bundled Chromium is stood in for by that binary).
* **Existing tests changed**: `test_resolve_kind` (a PDF is a document now, not binary) and the
  "bad scheme" integration test (FTP is a real source, so it became "unreachable FTP and missing
  file"). The M1/M2 expectation that an inline `var build=n` script keeps a short page static is why
  the auto-detection trigger above was tightened.
* **Qt in the sandbox** needs `libegl1 libnss3 libxkbcommon0 ...` (already noted for CI in M3).

### Manual checks that could not run here

* Launching Microsoft Edge (`msedge`) on Windows, including new headless mode and a Playwright
  driver subprocess under the engine's event loop.
* Housing Connect's live listings through the browser fetcher (needs the live site).
* `playwright install chromium` on first fallback (the sandbox blocks downloads; the installer is
  injected in tests).
* FTPS against a real server (explicit AUTH TLS and implicit TLS on 990).

### Known limits

* A PDF, or a user-supplied regex, can still run long inside a worker: the per-job timeout that kills
  and rebuilds the pool is M5.
* A page that becomes a JavaScript shell *after* its first check is not re-routed to the browser
  (change the method on the bookmark); detection is deliberately first-check only.
* Chromium limits very tall full-page captures (around 16,000 px); what a taller page yields is
  Chromium's behaviour, not something PageWatch controls or has tested.

## M5 · Unattended operation

### Scope and housekeeping

* **No new dependency, no schema migration.** psutil, httpx and the standard library cover
  everything. Facts the engine must remember (when the last backup ran) go in the existing
  `setting` table under a `_state.` prefix that `SettingsStore` never loads as a setting and
  `PUT /settings` cannot write.
* **Branch repair.** The branch tip was `e259c5e` (the SPEC upload); the M4 commit `90ca022` was a
  dangling commit on top of it, never attached to the branch. Fast-forwarded (lossless: the tip is
  its parent) before starting.
* **Opt-in machinery.** The power monitor, connectivity probe, maintenance loop and resource guard
  exist only when the engine is built with `enable_unattended` (the real entry point does), like the
  tray. A unit test therefore never probes a real network or starts a background loop it did not ask
  for. The old tests are unchanged; M5's own tests opt in with fakes.
* **Outside M5** (their milestones): the Settings and Problems screens (every M5 setting is reachable
  through `PUT /settings`), email/ntfy/export actions and their Problems entry (M6), plugins (M7),
  packaging (M8).

### Sleep, resume, catch-up

* **Three ways to notice a sleep**, any one is enough: wall clock minus monotonic time above 30 s;
  a 5 s heartbeat that fires more than 30 s late (Windows' monotonic clock may include suspended
  time, which hides the first signal: promised in M0); and `WM_POWERBROADCAST`. One wake produces
  several signals, so a 10 s debounce makes them one resume; after the sequence the heartbeat
  measures from the end of it (it can take two minutes, and that is not a second sleep: found by
  reading the code, before any test).
* **The resume sequence** is: hold scheduled dispatch → wait for the network (probe every 5 s, up to
  2 min) → plan the catch-up → release. The **order matters and a test caught it**: the first version
  released the hold *before* calling `catch_up`, so the scheduler woke on the release and started
  every overdue bookmark at once as plain `schedule` checks, and the catch-up found nothing left to
  stagger. The hold now stays until the overdue bookmarks have been re-timed. The same rule applies
  when the network returns from offline mode and when AC power returns (`catchup:<why>` holds).
* **Catch-up** gives each overdue bookmark one `catchup` check (the single due time per bookmark
  already means "not one per missed interval"), hotsites first, then longest overdue, spread over
  `min(catchup_spread_s, count × 1 s)` starting after the start-up delay. Checks already waiting to
  start are re-timed with the rest. A bookmark whose `days`/`window` forbid "now" is **moved to its
  next allowed start instead** (and `next_due_at` is updated), so a laptop that wakes at 03:00 does
  not check a bookmark limited to 07:00–23:00.
* **The same catch-up runs at start-up** (after a restart of several hours, and for a check that was
  interrupted by a crash) and when the network or AC power returns, not only after a sleep.
* **Start-up** takes one inline probe (never waits: an offline start goes to offline mode and keeps
  probing), holds dispatch until the catch-up is planned, and only then lets the scheduler run.
* **Windows window** (`SystemPowerBackend`): a hidden *top-level* window on its own thread, because
  message-only windows do not receive the broadcast messages. It also handles `WM_QUERYENDSESSION`
  (the engine stops itself). **This is the one piece of M5 that could not be run here.** Reviewing it
  found two mistakes before it ever ran on Windows: `ctypes.wintypes` has no `WNDCLASSW` (the first
  version would have raised and the feature silently never worked) and several `user32` calls lacked
  `argtypes`/`restype` (on 64-bit Python a window handle is truncated to 32 bits). It now declares
  its own structure and every signature, every `wintypes` name it uses is checked to exist, any
  exception only costs the OS notification (drift and heartbeat detection still work), and
  `os_power_events: false` turns the native window off. Listed under manual checks.

### Connectivity and offline mode

* **The probe** is a HEAD to `connectivity_url` (default Windows' own `msftconnecttest.com`
  endpoint) through the global proxy. **Any HTTP reply, whatever its status, means online**; only
  failing to get one (DNS, refused, timeout, TLS) means offline. It is single-flight, and its answer
  is reused for 2 s so a burst of failing checks causes one probe.
* **A failed check asks before it counts.** A DNS, connection or timeout failure (never for a local
  file) calls `verify()`; if the probe fails too the engine goes offline. While offline, a failing
  check is recorded as **`skipped` with reason `offline:<kind>`** rather than `error` (the spec says
  such failures "are not counted at all"; this also keeps the check log from filling with errors that
  were never the site's), the error counter and status are untouched, and the bookmark stays due and
  is caught up when the network is back. Verified: 10 simulated minutes offline leave every counter
  at 0 and raise no toast.
* **Known limits:** a captive portal answers the probe, so it counts as online; a wrong or blocked
  probe URL keeps the engine offline (visible in `/health`, the tray and the CLI `status`), which is
  what a configurable probe means.

### Battery

* `psutil.sensors_battery()` once a minute (no battery means not on battery). `on_battery: pause`
  *parks* a bookmark in the scheduler (it stays overdue and runs, once, when AC returns); a parked
  bookmark does not block others of the same host. **`slow` (×4) takes effect when a bookmark is next
  scheduled**, not by re-timing existing due times. The per-bookmark policy is cached in the
  scheduler and refreshed on create, patch, folder-default change and start-up.
* **Battery saver** (Windows, `GetSystemPowerStatus`) pauses everything only when
  `pause_on_battery_saver` is on (default off). **Keep awake** (`SetThreadExecutionState`, default
  off) is on only while the option is set *and* AutoWatch runs, and is set from the engine's
  event-loop thread because the call is per-thread.

### Errors and retries

* Retry-once and the error threshold are unchanged from M1. New: **marking a bookmark read clears
  `consecutive_errors` and the `error` status** (the spec's "or when the user opens the bookmark";
  the API's equivalent of opening is mark-read, which the UI, the toast button and the CLI use).
* **Per-job time limit** (deferred from M2 and M4): `worker_job_timeout_s` (120). A job past it has
  its worker processes killed (through `ProcessPoolExecutor._processes`, as the public
  `kill_workers` arrives only in Python 3.14), the pool rebuilt and `WorkerTimeout` raised; jobs that
  were running on the dead pool are retried once by the existing broken-pool path. In thread mode a
  thread cannot be killed, so it is abandoned and the executor replaced. The check fails with a
  `parse` error ("processing timed out"), so the bookmark turns `error` and notifies like any other
  failing source. Tested with a real worker process and with a real catastrophic regex
  (`(a+)+$`) in a process-mode engine. The parameter is called `time_limit` (not `timeout`) to satisfy
  ruff's ASYNC109.

### Durable action queue

* The M1 queue already persisted jobs, retried at 1/5/30 minutes, gave up after 5 attempts and
  survived restarts. What the spec says and M1 did not do: **a change's jobs now run one at a time in
  configured order** (a job waits while an earlier job of its change is running or waiting for its
  retry), and **`mark_read` always runs last**: `ActionsConfig` orders it last at the model boundary
  so `action_index` matches execution order everywhere.
* **A `mark_read` is skipped (job `done`, `last_error` says why) if an earlier action finally
  failed**: a change the user was never told about stays unread. The spec is silent; hiding an
  unsent alert looked worse than a surprising unread flag.
* The loop sleeps until the next scheduled retry (at most 60 s) instead of a flat minute, and is
  kicked on resume. Delivery stays at-least-once: a job interrupted by a crash runs again once (tested
  with a hung job and an engine stop), and a finished job is never sent again after a restart (tested).

### Retention and garbage collection

* **Keep rules**: pointer-referenced versions, pinned versions and the newest `keep_changed_versions`
  (20) per bookmark. A `change` goes with *either* of its versions (its jobs cascade), and the delete
  re-checks eligibility inside its own transaction, so a pointer that moved after the scan protects
  its version. Deletes are chunked (200) so the writer thread is never held for long.
* **Why the collector cannot delete something a worker is about to use.** A worker writes blobs before
  the row that references them and *skips* the write when the blob exists. So: `BlobStore` refreshes a
  blob's modification time on every dedupe hit (mtime means last use), and the sweep deletes only
  blobs that are unreferenced **and** unused for `blob_grace_h` (default 24 h, minimum 1 h: the
  setting cannot be made unsafe). A failing reference scan aborts the run without sweeping.
  Mutation-tested: removing the touch, or removing the nested-reference scan, each fails exactly the
  test that guards it.
* **Blobs referenced from inside other blobs**: a screenshot diff names its two screenshots and its
  overlay. Such a diff is recognised from its first decompressed bytes (a huge text diff is never
  read) and its references are marked.
* The reference set keeps 64-bit digest prefixes (about 5 MB at 1,000 bookmarks, about 50 MB for the
  nightly run at 10,000): a collision can only keep an orphan a night longer.
* **Disk cap**: while the blob store is over `disk_cap_gb`, prune the oldest unpinned,
  pointer-free versions until their blobs would add up to the excess, collect, look again (up to five
  rounds; shared blobs free nothing). If pointers alone exceed the cap it says so
  (`disk_cap_unreachable`) and deletes nothing else.
* `check_run` older than 30 days (spec) and `metric` older than 30 days (the spec is silent;
  `metric_retention_days`) are deleted; the WAL gets a passive checkpoint.

### Backup and restore

* **Zip contents**: `manifest.json`, an SQLite online backup (settings and macros are tables in it),
  `settings.json` and `macros.json` for readability, `plugins/`, and `blobs/` only when asked for. Never
  the lockfile, logs or secrets (tested: a token in `engine.lock` is nowhere in the zip).
* **Automatic backups** are `backup-auto-*.zip` and the newest `backup_keep` (14) are kept; manual,
  pre-migration and pre-restore copies are never auto-pruned. A job that has never run is due
  immediately, so there is a first backup two minutes after the first start, then one per 03:00.
* **Restore never changes live data in the running engine.** It validates (zip, safe member names, no
  path traversal, manifest, `PRAGMA integrity_check`, schema not newer than this build), stages the
  zip, and the engine exits with code 3. At the next start, before the database is opened, the current
  database is copied to `backups\pre-restore-*` (through SQLite's backup API, or as raw files if it is
  too damaged for that), then swapped in by a rename: the only step that cannot be undone. A staged zip
  that turns out bad is set aside as `restore\failed-*.zip` and the engine starts on the data it had.
* **A restored zip's `plugins/` are trusted code**, exactly like any file in the plugins folder (the
  spec: "plugins are trusted local code"). Restore only backups you made.
* **"The engine restarts itself after a restore"**: under Task Scheduler the task restarts it
  (`--supervised`); started by hand, the engine starts its own replacement, but refuses if it was
  itself started that way less than a minute ago, so a restart that does not help cannot loop. Tested
  with real processes both ways.
* **Added to the API**: `POST /backup` (`{include_blobs?, path?}`) and `POST /restore` (`{path}`,
  202); the spec lists both without bodies. No listing endpoint (the CLI and the UI can read the
  folder).

### Maintenance, memory guard, observability

* Backup at `backup_time` (03:00) and retention at `maintenance_time` (03:30), **"or at the next
  wake"**: due means the latest local occurrence of that time is newer than the last successful run,
  computed in the configured zone (DST-correct). A failing job is reported as a `problem` event and
  retried after an hour, not every minute.
* **"Engine RSS" is the engine process plus its children** (workers, browser): recycling the browser
  could not lower a process-only number. Over `rss_limit_mb` (1,500) the browser is recycled; still
  over 60 s later, the engine exits with code 3. CPU is the engine process's share of the whole machine.
* **Event-loop watchdog**: a thread, because the loop cannot report on itself while blocked. It logs
  the loop thread's stack once per stall (> 10 s). Tested with a real blocked loop; it also caught my
  own profiling script blocking the loop for 10 s.
* Hourly samples (`rss_mb`, `rss_engine_mb`, `cpu_pct`, `queue_length`, `in_flight`) go to `metric`.
  `/health` gained `rss_total_mb`, `cpu_percent`, `last_backup_at`, `last_maintenance_at` (additive).

### `service install`

* The task is registered from a Task Scheduler XML definition (only XML can set restart-on-failure,
  run-on-battery and no time limit): logon trigger for this user, no elevation, restart every minute
  (999 times, the most Task Scheduler accepts), `ExecutionTimeLimit` 0, allowed on battery, never a
  second instance. It runs `pythonw -m pagewatch.engine.main --supervised ...` (or `--exe`). Task
  Scheduler restarts on any non-zero exit, so exit 3 restarts and a clean Quit (0) does not; exit 2
  (a second engine) is also retried, which the engine's mutex makes harmless.
* The XML is generated and checked on every platform (`--print-xml` works anywhere, with no engine
  running); `schtasks` itself is Windows-only and a manual check.

### Soak and performance: what was measured, and what it found

A true 24-hour run cannot happen here, so there are two stand-ins (both opt-in, `-m soak`): a **simulated**
24 hours under the fake clock through the real scheduler, runner, pipeline, retention, backup and metrics
code, and a **real-time** sample of a real engine process.

* **Simulated day, 1,000 bookmarks at 1–60 minute intervals (62k checks):** queue peak 0, no stuck check,
  nobody late, zero errors, backups and retention ran, versions bounded, **no orphan blob after GC**
  (and every blob a version needs still present), database consistent. RSS 106 MB after two hours →
  **111 MB after 24 h (+4.0%)**.
* **That passing number took three real findings**, each of which the first runs got wrong:
  1. *A test double, mistaken for an engine leak.* The first run showed +375%: `ScriptedFetcher` keeps
     every request (each pinning its whole resolved configuration), `LogToastBackend` keeps every toast,
     and pytest keeps every log record. The soak now uses non-retaining stand-ins and silences log
     capture. (Confirmed by object counts, not assumed.)
  2. *A recurring nightly step.* +3, +7, +7 MB at each 03:00. The cause was not retention but the
     **backup running on a pooled reader thread**, whose page cache then kept a full copy of the
     database, one reader per night. Backup and retention's whole-table scans now use short-lived
     connections (a unit test asserts the backup never uses the reader pool, and fails on the old code).
  3. *A steady creep that tracked database size.* Memory-mapped I/O is off (checked), file-backed RSS is
     constant, and the growth is anonymous memory: SQLite's page caches, **20 MB × 5 connections** from
     M0, filling as the database grew (up to 100 MB over the first week). The caches are now 2 MB per
     reader and 4 MB for the writer. The 10,000-bookmark UI timings are unchanged (first page 41 ms,
     worst scroll page 103 ms, sorts 35–41 ms; the spec limit is 200 ms).
* **Real engine process, 1,000 bookmarks, browser closed, real worker processes:** idle CPU **0.23% of one
  core** with AutoWatch paused (spec: < 1%), 0.45% with hourly checks running. **RSS: 80 MB for the engine
  process, 302 MB for the whole tree** (the engine, 3 workers and the multiprocessing helper). The spec's
  "≤ 250 MB RSS at 1,000 bookmarks" is met if it means the engine process and **not met (by about 50 MB)
  if it counts the workers**; the spec does not say. This needs the owner's reading. If the tree is what
  counts, the levers are `worker_processes` (2 saves ~55 MB) or lazy imports in the workers.
* **Not measured, so not claimed:** 14 days of uptime and its "< 10% growth" (the simulated runs show the
  creep stopping after the caches fill and one small step per maintenance run; 72 simulated hours at 200
  bookmarks is the longest trend checked), the 2-hour capacity run at 10,000 bookmarks, and anything
  that depends on a real Windows machine sleeping.

### Tests

* **791 -> 905 default tests** (1 skipped: the permission test needs a non-root user), plus the 12
  real-browser tests from M4 (`-m browser`, unchanged, re-run) and 2 opt-in soak tests (`-m soak`). New:
  unit tests for retention (16, every keep rule, nested references, grace, disk cap, FK safety), backup and
  restore (26), the Task Scheduler definition (10), the guard and watchdog (12), maintenance (9), the
  worker time limit against a real process (3) and the backup/reader-pool regression (1); and integration
  tests for the whole engine under a fake clock (16: sleep, offline, battery, resume, restart), the action
  queue (8), backup and restore through the API and CLI (8), and real engine processes (5: kill -9,
  restore with self-respawn, supervised exit, the respawn guard).
* **Mutation-checked** (break the behaviour, watch the guarding test fail): blob reuse not refreshing the
  mtime; nested screenshot references ignored; the action queue not ordering a change's jobs;
  `mark_read` not forced last; the catch-up planned after the hold is released; offline failures counted;
  a backup going through a pooled reader.
* The kill -9 test is a real process: it stalls a check on the fixture site, `SIGKILL`s the engine,
  checks the database it left (consistent; the run still open; nothing half-written), restarts, and
  checks the interrupted run is closed as `error: interrupted` without touching the error counter and
  the bookmark is checked again (`catchup`).
* **Existing tests changed**: `test_openapi` (the schema is regenerated); nothing else.

### Manual checks that could not run here

* **Windows**: the hidden power window (suspend/resume/log-off), battery saver, keep-awake, a real laptop
  sleep and wake, `schtasks /Create` with the generated XML (and the task restarting the engine after
  exit 3), and the real `msedge` path from M4. This is where the unexecuted ctypes code lives.
* A real 24-hour soak, the 14-day uptime criterion, and the 10,000-bookmark capacity run.
* The engine's default connectivity URL on the owner's network (a corporate proxy or captive portal may
  change what "online" means).
