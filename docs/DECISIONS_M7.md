# Decisions and deviations: M7 · Power features

Written to `docs/DECISIONS_M7.md` (not `docs/DECISIONS.md`) so that the parallel M5 and M6 sessions
and this one never edit the same file. Fold the sections into `docs/DECISIONS.md` when the branches
are merged. The same goes for the module table and the data-flow notes under **Architecture
additions**, which stand in for the changes to `docs/ARCHITECTURE.md`.

Branch `claude/m7-power-features`, started from `90ca022` (M0 to M4).

## Reading of the request

SPEC *Milestones → M7* lists: Filter assistant in the UI, Follow-Links, merge pages, virtual
folders, import and export, reports, plugins with shipped examples, complete CLI. `docs/DECISIONS.md`
adds the Filter-Assistant bridge deferred from M3 and Follow-Links / merge pages deferred from M4.
The request also mentions "advanced selectors/rules"; there is no separate bullet for that in the
spec, so it is read as the rule side of those features: the assistant's selector, marker and text
rules (including widening to a parent element and choosing between CSS and XPath), Follow-Links'
include / exclude patterns (wildcard or regex), merge pages' next-page selector, and plugin gate rule 8
(`check_keywords`, `compare`).

**Not here, because they belong to the parallel sessions:** the `backup`, `restore` and
`service install|uninstall` CLI commands (M5; they are thin clients of M5's endpoints and a test
accepts their absence and their later arrival), and the M6 actions, logins, macros and proxies.

## Dependencies, schema, API shape

* **No dependency added.** `jinja2` (reports), `openpyxl` (XLSX import), `lxml`, `PySide6` (which
  ships `QtWebChannel`) were all there already. No `python-multipart`: `POST /import` takes JSON
  (`content`, or `content_base64` for a workbook) rather than a multipart upload.
* **No migration.** Things the features must remember (plugin state, the Follow-Links crawl, the
  "last report" marker) live in the existing `setting` table under `kv.<namespace>.<key>`
  (`engine/kv.py`). `SettingsStore` only reads keys that are fields of `Settings`, so those rows
  never reach the global settings, and `bookmarks.delete` removes a bookmark's rows with it. The
  alternative, `plugin_state` / `follow_*` tables, needed a migration file whose number could collide with
  one from M5 or M6.
* **Additive API.** New: `GET/POST /plugins`, `POST /plugins/install`, `POST /import`, `GET /export`,
  `GET /reports`, `POST /reports/{template}`, `POST /bookmarks/{id}/filter-assist`. New optional query
  `paths` on `GET /bookmarks/{id}/diff` and `GET /changes/{id}/render`. New models `FollowLinksConfig` and
  `MergePagesConfig` under `FetchConfig` (`fetch.follow`, `fetch.merge`, default `null`), and
  `ActionType.REPORT`. Nothing that existed changed shape. Per the working agreement the owner would
  normally be asked first; this session was told to proceed, so it is logged here instead.
* **`docs/openapi.json` is regenerated** (`python tools/gen_openapi.py`). It will conflict with the same
  file on the M5 and M6 branches; regenerate it after merging instead of resolving by hand.

## Virtual folders

* A virtual folder stores a `VirtualQuery` in `folder.query_json`. Criteria (all that are set must
  hold): `q`, `status`, `unread`, `enabled`, `folder` (a real folder with its subfolders),
  `changed_within_h`, `keyword_hits`, `source_type`, `check_method`, `plugin`, `min_errors`. An empty
  query is refused (it would be "everything" and is almost certainly a mistake).
* `GET /bookmarks?folder=<virtual id>` resolves it in the engine (`engine/virtual.py`), so the UI,
  the CLI and scripts need no knowledge of saved searches. A filter on the same request narrows it
  further; two that cannot both hold match nothing (`changed_since` is the exception: the later start
  satisfies both).
* `changed_within_h` is relative, so the answer changes as time passes (a plain `changed_since` would be a
  frozen instant).
* A saved search holds no bookmarks and no folders: placing a bookmark or folder in one is a 422, it
  has no `defaults`, and only a virtual folder has a `query`.
* `GET /bookmarks/counts` includes saved searches in `by_folder`, and `POST /check` accepts one.
* UI: a "Saved searches" branch before "Folders" (hidden until one exists), "New saved search" starts from the
  list as it is (search box, status, unread), edit and delete from the tree's context menu.

## Import and export

* **Import:** Netscape bookmarks HTML (folders from the `<H3>` / `<DL>` nesting; `javascript:`, `place:` and
  other non-pages are dropped), a URL list (names, `#` and `//` comments, bare `host.tld/path` get
  `https://`), CSV (any delimiter, a BOM, any column order, extra columns ignored), XLSX (first sheet), and
  a PageWatch export. The last is an addition: the spec lists only the first four, but an export you
  cannot read back is not much of a backup.
* `interval` accepts `90`, `90s`, `15m`, `2h`, `1d`, `1w`; a bare number is seconds, as in the CLI. Under a
  minute is a row error, as is a bad URL or keyword rule: the row is reported with its number and the rest
  import.
* **De-duplication** is by `engine/urls.py::normalize_url` (scheme and host case, default port, fragment,
  trailing slash), against the database and earlier rows of the same file. A different query string, `www.`
  or path case is a different page.
* `dry_run` reports what would happen and writes nothing (not even folders).
* A bookmark in a PageWatch export whose plugin is not installed on this machine is a row error ("plugin
  ... is not installed"), not a silent loss of the assignment: install the plugin and import again.
* **Export JSON** is versioned (`version: 1`; a newer file is refused with a clear message). It holds the
  stored overrides, not the effective values, so inheritance survives a round trip. Credentials are keyring
  entry names, so there is no secret to leak; a test checks the output for the word "password". A
  round-trip test deletes everything, imports the export and compares each bookmark's overrides.
* **OPML** lists bookmarks whose `source_type` is `feed` or whose latest content type is RSS / Atom, grouped by
  folder. **CSV** has a UTF-8 BOM (Excel needs it) and the columns the importer reads back.

## Reports

* Built-ins `changed`, `all`, `errors` in HTML, CSV, JSON and XML. HTML and XML are Jinja2 templates in
  `data/reports/`; CSV and JSON are written in code (correct quoting; no template to get wrong).
* Your own: `<data>/reports/<name>.<format>.j2`, sandboxed. A file named like a built-in replaces it.
  Escaping applies to HTML and XML only. (`select_autoescape` escapes every template rendered from a string,
  which turned `&` into `&amp;` in a CSV report; a test found it.)
* **The "last report" marker** is per template. A report moves it to the newest change it listed, and the
  next one lists changes strictly after it, so a change is reported once: never twice, never missed. (The first
  version used "now" and `>=`, which listed a change twice when it was detected at the instant of the report.)
  `mark: false` / `--no-mark` leaves it alone. With no marker, `changed` looks back 24 hours.
* **The `report` action** writes the report file when a change is detected (`path` with `{id}`, `{name}`,
  `{date}`, `{datetime}`, `{template}`; plain placeholders only, no attribute access). It never moves the
  marker: a burst of changes rewrites one living file instead of leaving an empty report behind the first. A
  misconfigured action fails at once (no retries) and is listed under Problems like the other failures.

## Plugins

* **Hooks and where they run.** `preprocess`, `check_keywords` and `compare` run in the worker process (they
  are part of the pipeline); `before_check`, `merge_pages`, `on_change` and `after_check` run on the engine
  in a thread, so a slow plugin cannot stall the event loop. `engine/plugin_runtime.py` (loading, `Session`)
  imports almost nothing so workers stay light; `engine/plugins.py` is the engine side.
* **Public API** is `pagewatch.plugin_api`: `Verdict`, the context, the helpers. `compare` returns
  `Verdict.change(reason)`, `Verdict.no_change(reason)`, a plain bool, or `None` to defer. `check_keywords`
  returns a bool or `None`.
* **Gate rule 8.** `check_keywords` is OR-ed with the built-in rules: a `True` is a hit labelled `plugin:<name>`;
  with no built-in rules a `False` makes rule 6 fail (`keyword_miss`), a `None` leaves the gate alone.
  `compare` runs last and has the final word: it can force an alert past a keyword miss and suppress one that
  would have passed. Rules 2 to 4 (bad fetches) still come first: no plugin can store an error page.
* **Parameters** are `name=value` settings in the bookmark's Info 1 to 3 or Note (`ctx.option("min")`), so
  the editor needs no per-plugin form and the settings travel with the bookmark.
* **State** is a JSON dict per bookmark (`kv.plugin_state`). Worker hooks get a copy and return the new
  one; the engine merges it and saves once the check is committed.
* **Isolation.** A hook that raises is logged and treated as absent; a coroutine hook is refused; one slower than
  5 s adds a warning; a plugin that fails to import is noted on every check. Notes go to `check_run.reason`
  (`plugin_error:<hook>`, `plugin_slow:<hook>`, `plugin_missing:<name>`, `plugin_skip`) **only when the check
  has no reason of its own**, so a gate reason is never hidden. A plugin cannot fail a stored check.
* `preprocess` is applied in `build_blocks` **and** in the viewer (`sources.view_bytes`), so highlights land
  on the blocks the extractor saw; re-assigning a plugin re-normalises the stored versions like a filter edit.
* `ctx.notify()` shows a toast (generic text for `private` bookmarks); `ctx.set_property()` may change
  `info1` to `info3`, `note` and `name`; `ctx.http_get` allows 10 calls per hook and 2 s between calls to a
  host (tests lower the gap).
* **Trust.** Plugins are loaded only from the data folder's `plugins/`, by a validated name, and installed only
  from the shipped examples (`POST /plugins/install`, never an arbitrary path or code from the request).
* **Shipped examples** (`data/plugins/`, installed with `pagewatch-cli plugin install <name>`): `price_range`
  (`check_keywords`), `stale_page` (`after_check` + state + `notify`), `single_new_item` (`compare`),
  `pdf_links` (`preprocess`). Each has an integration test that installs it through the API and drives real
  checks. `before_check`, `merge_pages`, `on_change` and the failure cases are covered by tests with plugins
  written in the test.

## Merge pages

* `fetch.merge` is a `next_selector` (CSS) or a `url_pattern` with `{n}` (relative patterns resolve against the
  previous page), `first_n` and `max_pages` (2 to 50, the first page included).
* The pages are concatenated **before** extraction: the first page whole, the others as
  `<div data-pw-page="N">` inside its `<body>`, so page 1 keeps its DOM paths. Each page is decoded with its
  own charset. The merge is what is stored, hashed and shown.
* **Every page is fetched every time**: conditional GET is switched off while merging (a 304 on page 1 says
  nothing about page 2).
* A page that fails fails the check; a partial merge would look like removed content. In pattern mode a
  404 or 410 ends the walk (the pattern ran past the last page); so does a repeated body, a repeated URL, the
  page limit, and a merged size over `max_bytes`.
* `merge_pages` plugin hooks add to this; both are joined in one worker call.

## Follow-Links

* After a check that stored a new version of a web page, a worker extracts its links and the crawl filters
  them: same site (a leading `www.` ignored; `same_host: false` lifts it), include / exclude patterns
  (wildcard or regex, matched anywhere in the URL, any case), not the page itself, not seen before, not already
  a bookmark, **allowed by robots.txt**. A page with `<meta name="robots" content="nofollow">` yields nothing.
* **robots.txt** follows RFC 9309: a 4xx means "no file, everything allowed"; a 5xx or an unreachable host means
  nothing is allowed for now (asked again after 15 minutes); a file is cached for 24 hours. The `PageWatch`
  group wins over `*`. It applies to **discovered** links only: a page the owner chose is never checked.
* **Children** go in one generated folder next to the parent, named `<name> (followed)`. The folder's defaults
  are the parent's stored schedule, fetch (without `follow`, `merge`, `records`), filter, gate and actions plus
  its method, highlight mode and priority; they are refreshed on every discovery and whenever the parent is
  edited, so editing the parent's keywords reaches its children ("inherits the parent's settings", kept live).
  The parent's plugin and macro are copied to each child. The first check of a child has trigger `follow`.
* **Depth and the page limit.** `depth` (default 1, up to 5): each child remembers its root and the levels left
  (`kv.follow_lineage`) and discovers links of its own page in turn. `max_pages` (default 50) is **one budget for
  the whole crawl**, counted on the root, and it only goes down: a deleted child does not free a place and is
  never created again (`kv.follow_state` remembers every URL). Discoveries of one crawl take turns on a
  per-root lock. The acceptance test found why: with a lock per page, three sections discovered at once
  each saw the same budget and 9 pages were created against a limit of 5.
* Editing the follow settings looks at the stored page again, so raising `max_pages` needs no page change.

## Filter assistant

* **Engine.** `?paths=true` tags every element of the Highlighted and New views with `data-pw-path`, the path the
  extractor gives blocks, computed before any `<ins>` / `<del>` is injected (they would shift sibling
  indices). Off by default, so existing output is unchanged. `POST /bookmarks/{id}/filter-assist` takes that path
  (and an end path), the selected text, a version (`new`, `old` or a `version_id`) and `levels_up`, and returns
  up to five choices, each with its rule, a ready `PATCH` body, an explanation and warnings. Nothing is saved.
* **The five rules.** Ignore element, watch only element (a selector, CSS when a stable one exists, else the
  absolute XPath; widen to a parent up to the body); ignore text like this (a regex, digits as `\d+`, spaces as
  `\s+`, applied page-wide); ignore from page start to here / from here to page end (`between` with an open end,
  inclusive).
* **Markers.** `between` rules start or end at the *first* occurrence, so a marker that also appears earlier
  would cut in the wrong place. A marker is checked against the filtered blocks and extended to the whole
  block when that makes it unique; otherwise the choice carries a warning, as it does for text that is not in
  the filtered page. Multi-line selections use the first line to start and the last to end.
* **Rules are appended to the effective list** (folder defaults included). A bookmark-level list replaces an
  inherited one, so appending to the stored list alone would silently drop the folder's rules. (The existing
  false-positive flow appends to the stored list and has that flaw; left alone here.)
* **Test filter before saving.** The popup asks `POST /bookmarks/{id}/test-filter` for the selected choice and
  shows how much of the page is left (against the page as it is now), the effect on the change being looked
  at ("disappears", "1 changed block remains, would alert", "would not alert (keyword_miss)") and a preview of
  the resulting text.
* **Bridge.** `ui/web/bridge.js` runs in an isolated world (`QWebEngineScript.ApplicationWorld`) over
  `QWebChannel`, so the page (scripts are stripped anyway) can neither see nor call it, and the page's strict
  CSP does not apply to it. Alt + select, right-click (on a selection or a bare element) and Ctrl+Shift+F
  (the keyboard route) all report `{text, path, end_path}`. It is inert in any document without `data-pw-path`
  (the Text diff view).
  * **Qt quirk, found by a test:** `QWebEngineScript`s are injected into the *first* document only; a second
    `setHtml` into the same page never sees them, and the bridge silently died after the first reload. The
    scripts are therefore run again after every `loadFinished`.
  * The bridge builds the selected text itself, **without the struck-through removed text** of the
    Highlighted view (`String(selection)` returned "Updated 56 minutes ago" for a 5 to 6 change), and
    moves a triple-click's end (offset 0 of the next block) back onto the text the user meant.
* A selection made while the history list shows an older alert is looked up in *that* page (`version_id`).

## CLI

`report`, `import`, `export`, `plugin list|install`, plus `add --plugin / --follow* / --merge*` and
`folder add --virtual --query`. They live in `cli/power.py`; `main.py` gains five hook lines. The spec's other
commands are present except M5's three. `report` and `export` write the file given with `--out` / as the
argument (or print), and `--json` prints a small result object.

## UI

File menu (Add, Import / Export, New saved search), Tools menu (Filter assistant, False positive), the
Import / Export dialog, the saved-search dialog and the assistant popup. The bookmark editor gained a Plugin
selector (General), Follow-Links and merge pages (Advanced); they round-trip without inventing changes, and
switching Follow-Links or merging off keeps the rest of the settings (`enabled: false`) rather than deleting
them. The "Saved searches" tree root sits before "Folders" so the existing tree test and layout still hold.

## Tests

Default suite: **791 passed + 1 skipped at `90ca022` -> 1,009 passed + 1 skipped (218 new tests)**; the 12
real-Chromium tests (`-m browser`) still pass. New: virtual folders, import / export (parsers, API,
round trips), reports, plugin runtime / hooks / catalogue / shipped examples / a real worker process, merge
pages, Follow-Links (including the acceptance scenario), the filter assistant (engine, bridge in a real
`QWebEngineView`, popup, full flow against a real engine), saved searches, import / export dialogs, the
editor, and the CLI. Qt tests need `libegl1 libnss3 libxkbcommon0 ...` as before.

Two things a failing UI test taught: a test that leaves a `QWebEngineView` alive when it fails makes the
interpreter hang at exit (QtWebEngine wants every view destroyed before the `QApplication`), so a UI failure
shows up as a *timeout*; and the bridge-only tests must detach `MainWindow._open_assistant`, or the first
Alt+select opens a real modal dialog.

## Architecture additions (for `docs/ARCHITECTURE.md`)

| Module | Role |
| --- | --- |
| `engine/kv.py`, `engine/urls.py` | Namespaced records in the `setting` table; URL identity for de-duplication |
| `engine/virtual.py` | Saved searches: query model to `bookmark_query` filters, narrowing, counts |
| `engine/portability.py` | Import parsers and importer; JSON / OPML / CSV exporters |
| `engine/reports.py`, `data/reports/`, `engine/actions/report.py` | Report data and rendering (sandboxed Jinja2), the `report` action |
| `plugin_api.py` | What a plugin imports and receives (`Verdict`, context, helpers) |
| `engine/plugin_runtime.py` | Loading plugin files, `Session` (hook calls), shared with worker processes |
| `engine/plugins.py` | `PluginHost` (catalogue, install, state) and `EngineSession` (hooks on the engine, effects) |
| `data/plugins/` | The four shipped example plugins |
| `engine/pipeline/merge.py`, `pipeline/links.py` | Next-page link and concatenation; link extraction (workers) |
| `engine/follow.py`, `engine/robots.py` | Follow-Links crawl, folders, budget; robots.txt cache |
| `engine/pipeline/assist.py`, `engine/assistant.py` | Filter assistant: selection to rules (worker), choices and patches |
| `ui/web/bridge.js`, `ui/windows/filter_assistant.py` | Selection bridge; the assistant popup |
| `ui/windows/saved_search.py`, `ui/windows/import_export.py` | Saved-search and import / export dialogs |
| `cli/power.py` | The M7 CLI commands and options |

```
runner.run
  ├─ plugin: open session (state from kv)  → before_check (skip / rewrite the request)
  ├─ fetch → merge pages (built-in next link / pattern, then the plugin's merge_pages) → one document
  ├─ worker: process_check  [preprocess] → blocks → diff → gate rules 1–7
  │                          → check_keywords (OR) → … → compare (last word)      ← plugin state/props/alerts return
  ├─ commit (check_run.reason ← first plugin note if nothing else)
  ├─ on_change / after_check → state saved, properties patched, toasts
  └─ Follow-Links: new version → discover (robots, patterns, budget) → children in the generated folder
```

## Manual checks that could not run here

* The bridge was exercised in the sandbox's headless QtWebEngine (Alt+select and right-click as synthetic
  events, plus real keyboard shortcuts). Real mouse input on Windows (Alt+drag, the right-click menu
  suppression, high-DPI) and the look of the popup have not been seen by a person.
* Windows paths in the `report` action and import / export of non-ASCII file names.
* Import of real browser exports from Edge, Chrome and Firefox (the fixture is Chrome's layout).
* robots.txt against a live site; Follow-Links on a large real site.

## Known limits

* A plugin cannot be interrupted: a hook that never returns holds a thread (or a worker) forever; the 5 s
  rule only warns. The per-job worker timeout is M5's.
* `merge_pages`, `before_check`, `on_change` and `after_check` plugins run on the engine for any bookmark that
  has one, one thread per call; many busy plugins share the default thread pool.
* Plugin state, Follow-Links memory and the report marker are included in a backup only if the backup copies the
  `setting` table (the planned SQLite online backup does).
* Follow-Links checks robots.txt when it *creates* a child, not again at every check of that child.
* Packaging (M8) must ship `data/reports/*.j2`, `data/plugins/*.py` and `ui/web/bridge.js` as data files.
* `Verdict.change()` from a plugin overrides keyword and threshold rules but not the bad-fetch rules, by design.

## Merging with M5 and M6

Shared files edited here (expect small textual conflicts, none of them logical):
`models.py` (new models; `ActionType.REPORT`), `engine/runner.py` (plugin session, merge, follow hook, a
skipped outcome), `engine/core.py` (`plugins`, `follow`, `source_cfg`), `engine/bookmarks.py` (virtual /
plugin checks, delete cleanup, follow hook), `engine/api/app.py` (router includes), `engine/actions/builtin.py`
(one line), `cli/main.py` (five hook lines) and `cli/client.py` (`raw`), `ui/windows/main_window.py` and
`bookmark_editor.py`, `ui/client.py`, `ui/models.py`, `docs/openapi.json` (regenerate). Everything else is new
files. If M5 adds a `Check.skipped` path or M6 changes `_fetch`, keep the order: plugin `before_check`, fetch,
merge pages, pipeline.
