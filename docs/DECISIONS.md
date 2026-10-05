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
