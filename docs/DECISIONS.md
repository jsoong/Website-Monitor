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
