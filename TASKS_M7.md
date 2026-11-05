# TASKS: Power features (milestone M7)

Branch `claude/m7-power-features`, started from `90ca022` (M0 to M4 complete, 791 tests green).
This file and `docs/DECISIONS_M7.md` stand in for `TASKS.md` / `docs/DECISIONS.md` /
`docs/ARCHITECTURE.md` on purpose: M5 and M6 are built in parallel sessions and would conflict on
those files. Fold both into the shared documents when the branches are merged.

Scope, from SPEC.md *Milestones → M7*: Filter assistant in the UI, Follow-Links, merge pages,
virtual folders, import and export, reports, plugins with shipped examples, complete CLI.
Acceptance: Follow-Links on the fixture site creates child bookmarks within its depth and page
limits; each shipped plugin example passes its integration test. DECISIONS.md (M3) deferred the
Filter Assistant with Alt+select and the QWebChannel bridge to this milestone, and (M4) deferred
Follow-Links and merge pages.

Out of scope (owned by the parallel sessions): resume / offline / catch-up / battery / retention /
automatic backup / `service install` (M5); email, ntfy, export, run-program, scrape, script
actions, logins, macros, proxies, keyring (M6). `backup`, `restore` and `service` CLI commands are
M5's and are not duplicated here.

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

- [x] Z1 `docs/DECISIONS_M7.md` (decisions, deviations, architecture additions, manual checks).
- [x] Z2 `docs/openapi.json` regenerated.
- [x] Z3 ruff, mypy `--strict`, full suite green: 1,009 passed + 1 skipped (was 791 + 1), 12 passed with `-m browser`.
- [x] Z4 Commit, push to `claude/m7-power-features`, stop.
