# TASKS: Sources domain (milestone M4)

Scope: SPEC.md *Fetch layer* (fetchers, method auto-detection, browser management, records sources),
*Change detection → Screenshot comparison*, *Desktop UI → change viewer* (the Screenshot diff tab
that M3 left as a placeholder), and the M4 acceptance criteria.

Out of scope (later milestones, not touched here): resume/connectivity/catch-up/battery/retention/
backup/service install (M5); logins, Check-Macros, cookie import, proxies per bookmark, keyring,
per-host overrides, Basic/Digest auth (M6); Follow-Links, merge pages, Filter Assistant, plugins,
import/export (M7). Nothing in this list schedules or runs unattended work.

Rules from SPEC "Working agreement": one milestone, deviations logged in `docs/DECISIONS.md`,
ARCHITECTURE.md updated, full suite green, one commit. Dependencies added: only those the spec's
technology table names (playwright, pdfminer.six, python-docx, openpyxl, feedparser, aioftp).
No schema migration is needed (all columns already exist).

## Audit: SPEC vs repository at 652cbc7

| SPEC requirement | State in the repo | Task |
| --- | --- | --- |
| `FetchResult` with `screenshot_png`, `not_modified`, error kinds incl. `browser` | Present (`fetch/base.py`, `FetchErrorKind`) | reuse |
| static fetcher | Done (`fetch/static.py`) | reuse |
| browser fetcher, browser management (1 shared browser, lazy, Edge then bundled Chromium, 3 pages, 10 min idle, 500-page / 3-crash recycle, block images/media/fonts, 45 s) | Missing; `/health.browser_state` exists but is never set | S3.4, S3.6 |
| screenshot fetcher (1366×900, scale 1, animations off, full-page or clip) | Missing | S3.5 |
| document fetcher: PDF, DOCX, XLSX → HTML (XLSX capped at 5,000 rows) | Missing (`resolve_kind` knows only html/json/text/binary) | S2.1, S2.2 |
| feed fetcher: one block per entry keyed by id or link; optional enclosure download | Missing | S2.3, S3.3 |
| ftp fetcher: FTP/FTPS files and listings | Missing | S3.2 |
| file fetcher: mtime+size shortcut, folders as listing tables, optional recursion | Missing; runner rejects any non-http(s) URL | S3.1, S3.7 |
| records sources: JSON/CSV, JSONPath, ID field, row filter, watched fields, new/changed/removed events | Missing | S1.3, S2.4, S2.6 |
| method auto-detection: static first; < 200 chars or script-only shell → browser, persist `browser`, log why | `detect.needs_browser` exists, nothing uses it; runner always static | S3.8 |
| `check_run.method` records how content was obtained | Hard-coded `static` | S3.7 |
| screenshot comparison (grayscale, ignore rects, > 24/255, dilate, regions, `min_ratio` 0.2 %, height > 5 %, overlay PNG) | Missing | S4.1, S4.2 |
| `version.screenshot_hash` stored | Column and `NewVersion.screenshot_hash` exist; never filled | S4.3 |
| `GET /changes/{id}/render?view=screenshot` ("sanitized HTML or PNG") | Stub that answers 404; unread diff rejects the view | S4.4 |
| Viewer *Screenshot diff* tab | Placeholder `QLabel` | S4.5 |
| `POST /preview` with method `auto` (detects JS app, renders it) | Static only; reports `method: browser` but never renders it | S5.1 |
| Golden corpus cases for PDF, RSS, records, JS-rendered pages ("added with M4") | Absent | S6.2 |
| Browser tests marked `browser` | Marker declared, no tests | S6.4 |

## S0 Setup

- [x] S0.1 Add the six named dependencies (uv). `libegl1` etc. installed in the sandbox so the Qt tests run.
- [x] S0.2 Baseline: 446 tests green at 652cbc7.

## S1 Models and settings (`models.py`)

- [x] S1.1 `BrowserOptions` under `FetchConfig.browser`: `delay_after_load_s`, `scroll_count`, `mouse_moves`, `keys`, `full_page`, `clip`.
- [x] S1.2 `FeedOptions` (`summary`, `max_entries`, `download_enclosures`, `enclosures_dir`) and `FileOptions` (`recursive`, `max_entries`) under `FetchConfig`; `FtpOptions` (`recursive`, passive/secure flags via URL scheme).
- [x] S1.3 `RecordsConfig` under `FetchConfig.records`: `format`, `path`, `id_field`, `filter` (validated expression), `fields`, `events`; required when `source_type=records`.
- [x] S1.4 `FilterConfig.screenshot`: `ignore` rectangles, `min_ratio` (0.002), `height_change_pct` (5).
- [x] S1.5 `Settings.browser_channel` (`msedge`), `browser_executable`; API bodies (`PreviewRequest` already carries `check_method`).
- [x] S1.6 Regenerate `docs/openapi.json`.

## S2 Source decoding in the worker pipeline

- [x] S2.1 `resolve_kind` / `detect.classify`: pdf, docx, xlsx, feed, image, records by explicit `source_type`, content type, URL extension, then magic bytes.
- [x] S2.2 `pipeline/documents.py`: PDF (text per page via pdfminer.six), DOCX (paragraphs and tables, in order), XLSX (one table per sheet, 5,000-row cap with a warning) → deterministic HTML.
- [x] S2.3 `pipeline/feeds.py`: feedparser → one `<li>` per entry (title + summary), de-duplicated by entry id or link.
- [x] S2.4 `pipeline/jsonpath.py` (subset), `pipeline/records.py`: row filter language, JSON/CSV rows → one block per record (ID first, watched fields in stable order), events new/changed/removed.
- [x] S2.5 Wire into `build_blocks`, viewer `render_plain` / `render_highlight` (documents and feeds highlight in place; records fall back to the text view), image blocks.
- [x] S2.6 Gate: records `events` filter (`records_events` reason) and a summary that names the records.

## S3 Fetchers (event loop)

- [x] S3.1 `fetch/localfile.py`: files (mtime+size → `not_modified`), folders → listing table, optional recursion, size cap.
- [x] S3.2 `fetch/ftp.py`: aioftp, FTP and FTPS, file download or listing table; password via a `SecretStore` (keyring-backed store arrives in M6).
- [x] S3.3 `fetch/feed.py`: feed fetch on the static client + optional enclosure download.
- [x] S3.4 `fetch/browser.py`: `BrowserManager` (lazy launch, msedge → bundled Chromium fallback, ≤ 3 pages, idle close, recycle, resource blocking, 45 s timeout, injectable driver and clock) and `BrowserFetcher` (load + delay, scroll, synthetic input, same-origin iframes inlined).
- [x] S3.5 `fetch/screenshot.py`: fixed viewport, animations off, full-page or clipped PNG.
- [x] S3.6 `fetch/select.py` dispatch by URL scheme, `source_type` and `check_method`; `Engine` owns the fetchers, closes them on stop; `/health.browser_state`.
- [x] S3.7 Runner: dispatch, real `check_run.method` (static, browser, screenshot, document, feed, ftp, file, records), 304 for files.
- [x] S3.8 Auto-detection on the first check: re-run in the browser, persist `check_method=browser`, record why (`check_run.reason`, log), tell the scheduler to use the browser pool.

## S4 Screenshot capture and diffing

- [x] S4.1 `pipeline/screenshot.py`: grayscale, ignore rectangles, threshold 24/255, dilate, group into regions (Pillow only), `min_ratio`, height change > 5 %, red-box overlay PNG.
- [x] S4.2 Screenshot method in `process_check`: PNG blob, screenshot-hash shortcut, below-noise → unchanged, gate-diff JSON + overlay blob, summary.
- [x] S4.3 `VersionRef.screenshot_hash`; the runner stores `version.screenshot_hash`.
- [x] S4.4 API: `view=screenshot` with `format=png|html|json` on `/changes/{id}/render` and `/bookmarks/{id}/diff` (baseline → latest, computed on demand).
- [x] S4.5 UI: the Screenshot diff tab shows the overlay (region count, ratio), with clear empty/error states; `ApiClient` support; add-assistant text no longer promises "later".

## S5 Add-bookmark preview

- [x] S5.1 `POST /preview`: use the same dispatch; under `auto`, a JavaScript app is re-fetched in the browser and the rendered page is shown; feeds/PDF/DOCX/XLSX recognised.

## S6 Tests

- [x] S6.1 Unit: models, jsonpath, row filter, records, documents, feeds, `resolve_kind`, screenshot diff, browser manager (fake driver + fake clock), local files, FTP (aioftp server), dispatch.
- [x] S6.2 Golden corpus (M4 cases): PDF revision, DOCX edit, XLSX cell, RSS new item, records new/changed/removed, rendered JS page.
- [x] S6.3 Integration (engine + fixture site/FTP/files, fake browser): one alert for a new lottery, feed item, PDF revision, folder listing, FTP file, auto-detection switch, screenshot flow, screenshot API, `/health.browser_state`.
- [x] S6.4 `browser`-marked tests against real Chromium: JS-only fixture switched automatically, screenshot diff boxes the changed region, scroll and iframe inlining, idle close, fallback when the first choice will not launch.
- [x] S6.5 UI test: Screenshot diff tab.

## S7 Finish

- [x] S7.1 `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (M4 section), `docs/openapi.json`.
- [x] S7.2 ruff, mypy `--strict`, full suite green: 791 passed + 1 skipped (default), 12 passed (`-m browser`).
- [x] S7.3 Commit, push to `claude/focused-mccarthy-2a74te`, stop.

## Added along the way (not in the first plan)

- [x] CLI: `add --type` and `add --fetch '<json>'` so records, feed, PDF and FTP sources can be created headlessly.
- [x] Editor: browser options, records JSON, screenshot comparison (threshold, height, ignore rectangles); round-trip tests.
- [x] Viewer: per-request sequence numbers so a late response can no longer overwrite a newer one (found by the new tab's test).
- [x] Auto-detection trigger tightened (external or large inline script, mount point, or "needs JavaScript"), see DECISIONS.
- [x] `tools/gen_golden_documents.py` regenerates the binary golden fixtures.

## M4 acceptance criteria → where they are proved

| Criterion (SPEC M4) | Proof |
| --- | --- |
| The JS-only fixture is detected and switched to browser automatically | `tests/integration/test_m4_browser_flow.py` (scripted browser, default suite) and `tests/browser/test_chromium.py` (real Chromium) |
| Screenshot diff boxes the changed region on the visual fixture | `tests/unit/test_screenshot_diff.py` (synthetic), `test_m4_browser_flow.py` (whole engine) and `test_chromium.py` (real page, real box position) |
| The PDF revision fixture highlights the changed paragraph | golden case `pdf-revision` (snapshot) and `test_m4_sources.py` (the highlight HTML marks exactly that paragraph) |
| The browser closes after 10 idle minutes; falls back to bundled Chromium when Edge will not launch | `tests/unit/test_browser_manager.py` (fake browser, fake clock) and `test_chromium.py` (real Chromium; Edge really is absent here) |
| A new lottery in the records fixture produces exactly one alert naming it | `test_m4_sources.py::test_a_new_lottery_produces_exactly_one_alert_naming_it` and golden `records-new-lottery` |
| Housing Connect's live listings render through the browser fetcher | **Manual check** (needs the live site); not run here |

## Manual checks that cannot run in this sandbox

- Launching Microsoft Edge (`msedge` channel) on Windows; the sandbox has only Playwright's Chromium.
- Housing Connect's public listings through the browser fetcher (live site).
- `playwright install chromium` on first fallback (the sandbox forbids downloads).
