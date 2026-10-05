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
