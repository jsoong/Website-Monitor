# TASKS: Logins and proxies (milestone M6, second half)

Branch `claude/m6-logins`, started from `aadd380` (M0–M5, including M5 part 2: the Settings and
Problems screens). The first half of M6, the actions, is on `origin/claude/m6-actions` (`775f56c`,
built from `90ca022`, so it does not contain M5). This session builds exactly the half that branch
deferred and **does not merge or edit that branch**; see *Merge notes*.

`TASKS_M6.md` on that branch lists the unbuilt items: macros, semi-attended sign-in, cookie import,
per-bookmark proxies, per-host overrides, and the portal experiment. Those are this file.

## Scope decision (read this first)

| Unbuilt M6 item (from `TASKS_M6.md` and SPEC) | State now |
| --- | --- |
| Check-Macro **replay** (goto, fill, click, select, press, wait_for, assert_text; secrets by key) | **Done**, real Chromium and HTTP-backed double |
| Check-Macro **recorder** ("Record macro" -> Playwright codegen -> steps JSON) | **Done** (converter and subprocess flow tested; **a real codegen window was not run here**) |
| **Session reuse** (macro runs only when a login signal appears) | **Done** |
| **Semi-attended sign-in** ("Sign in" window on the bookmark's profile, immediate check, daily prompt) | **Done** (window tested in new headless mode; **a visible window was not seen here**) |
| **Session expiry**: one needs-login alert, bookmark paused | **Done**, restart-safe |
| **Cookie import** (Netscape `cookies.txt` per bookmark) | **Done** (static and browser) |
| **Server auth** Basic / Digest from the secret store | **Done** (static and browser) |
| **Per-bookmark proxies** (HTTP, HTTPS, SOCKS5) and the global proxy | **Done** (with a stored proxy login). HTTPS-to-proxy TLS was not exercised |
| **Per-host overrides** | Concurrency and spacing overrides **already existed** (M5 part 2). Added what was missing: key normalisation, the scheduler stretching an over-limit host, the editor warning |
| **Stored-password login** rules (opt-in, 6 h, none after a failure) | **Done** (they are what the spec says must exist before any automated login) |
| **Portal experiment** | **The instrument is built and tested. The experiment itself has NOT been run**, and no findings are recorded. See `docs/DECISIONS_M6_LOGINS.md` |
| Never request an ACCESS HRA notice PDF | Proven against a fixture portal (request log). **The manual check against the real portal is still the owner's** |
| Signed-in bookmarks default to `alert_privacy: private` | **Done** for every way of becoming signed in (the actions branch only handles creation) |

Not in this session: the email/ntfy/webhook/export/run-program/scrape/script actions (the other half
of M6), and anything from M7.

## Audit: SPEC vs repository at aadd380

| SPEC requirement | State at `aadd380` | Task |
| --- | --- | --- |
| Basic / Digest auth, credentials from the keyring | `AuthConfig` model only; FTP read it, HTTP did not; `SecretStore` had no real store | S2.2, S1.3 |
| Cookie import, one `cookies.txt` per bookmark | `FetchConfig.cookies_file` field, never read | S2.3 |
| Global and per-bookmark proxy (HTTP, HTTPS, SOCKS5) | `fetch.proxy` honoured by httpx only; the browser used only the global proxy; no validation, no SOCKS (no `socksio`), no proxy login | S2.1 |
| Check-Macros; `macro` table | Table and `bookmark.macro_id` existed; no steps model, API, runner or UI | S3 |
| "Bookmarks with logins get a persistent context in `profiles\<id>\`" | `profiles/` folder existed; every page shared one ephemeral context | S3.2 |
| Macro runs only when a login signal appears | Nothing | S3.3 |
| Recording with Playwright codegen | Nothing | S3.6 |
| `needs_login` status | Enum value, UI colour and Problems line existed; **nothing ever set it** | S4 |
| Semi-attended sign-in; daily prompt; wipe session | Nothing | S4 |
| Per-host: 2 concurrent, >= 2 s, overridable | Done in `HostGate`/Settings (M5 part 2) | verified, S5 |
| "...the editor warns and the scheduler stretches that host's intervals evenly" | Explicitly deferred by M5 part 2 | S5.2, S5.3 |
| Portal experiment before any automated login | Nothing | S6 |

## S0 Setup

- [x] S0.1 Branch `claude/m6-logins` from **`aadd380`** (see DECISIONS: the checked-out branch was one commit behind it).
- [x] S0.2 Environment: Qt system libraries, `uv sync --extra ui`; baseline 956 passed + 1 skipped; `mypy --strict` clean.
- [x] S0.3 Read `origin/claude/m6-actions:TASKS_M6.md` and SPEC; this file and `docs/DECISIONS_M6_LOGINS.md` written.

## S1 Models, secrets, schema

- [x] S1.1 `models.py`: `MacroStep` union (`GotoStep` ... `AssertTextStep`), `SecretRef`, `LoginSignal`, `MacroDef`/`MacroOut`, `LoginConfig`, proxy fields and `check_proxy_url`, `normalize_host`, new `Settings` fields, API bodies (cookies, login status, sign-in, recorder, experiments).
- [x] S1.2 A password field cannot be filled with an inline value (the model refuses it); secret names are restricted; a proxy address cannot carry a password.
- [x] S1.3 `engine/secrets.py` (keyring-backed store) **identical to the actions branch's file**; `Engine` default store as there; `GET/PUT/DELETE /secrets/{name}`, write-only.
- [x] S1.4 Migration `0002_logins.sql`: `login_state` (per-bookmark sign-in memory). Nothing else in the schema changes.
- [x] S1.5 Dependencies: `keyring` (named by the spec) and httpx's `socks` extra (SOCKS5 is in the spec). Flagged in DECISIONS.

## S2 Fetch layer

- [x] S2.1 `fetch/proxy.py`: bookmark proxy, else global; password from the secret store; static and browser.
- [x] S2.2 Static fetcher: Basic/Digest; refuses Basic over plain http to a non-local host; a 401 for a bookmark that sent credentials is `auth_rejected`.
- [x] S2.3 `fetch/cookies.py`: Netscape parser (HttpOnly prefix, spaces, BOM, expiry), httpx jar, Playwright cookies; a private session (client + jar) per signed-in bookmark; import / status / remove API (owner-only file).
- [x] S2.4 `FetchResult` gains `login_required`, `login_reason`, `macro_ran`, `macro_error`; `FetchRequest` gains `bookmark_id`, `login`.

## S3 Macros and browser sessions

- [x] S3.1 `fetch/macro.py`: the interpreter. Errors are built from the step, never from Playwright's text (which can echo a typed value).
- [x] S3.2 `BrowserManager`: `SessionSpec`, persistent profiles (own process), isolated contexts for cookies / proxy / server login, `launch_profile`; **session cookies saved beside the profile and restored** (Chromium drops them on close; found by a real-browser test).
- [x] S3.3 `fetch/login.py` + `BrowserFetcher`: signal on the live page; macro only when it shows, only if `plan.permit_macro()` allows; re-load and re-check afterwards.
- [x] S3.4 `Logins._permit`: stored-password double opt-in, one per `min_login_interval_s`, none after a failure; the attempt is recorded *before* the macro runs.
- [x] S3.5 `/macros` CRUD; bookmark validation (macro exists, browser method, a login signal exists).
- [x] S3.6 `logins/recorder.py`: codegen JSONL -> steps (a typed password is dropped, replaced by `{"secret": key}`), subprocess runner, `/macros/record`.

## S4 needs_login, sign-in, prompt

- [x] S4.1 Runner: `_login_gone` / `_needs_login`: status `needs_login`, `check_run` = error with a reason code, **no error counter**, **one** alert (only the transition), `next_due_at` NULL.
- [x] S4.2 Scheduler load and `schedule_bookmark` keep a `needs_login` bookmark paused across restarts; demand and Problems agree.
- [x] S4.3 `logins/signin.py`: visible window on the profile, watcher for "owner closed it", immediate check; scheduled checks skip (`signing_in`) while it is open.
- [x] S4.4 `logins/prompt.py`: the daily offer (never a keep-alive, never signs in by itself).
- [x] S4.5 Resume, wipe session, login status endpoints; signed-in bookmarks default to private (create and patch).
- [x] S4.6 Deleting a bookmark deletes its profile, imported cookies and in-memory jar (they are secrets), and closes an open sign-in window.

## S5 Per-host overrides

- [x] S5.1 Override keys normalised (`https://Example.COM:8443/x` -> `example.com`) in the model, in `PUT /settings` (so removal works under any spelling) and in the Settings table.
- [x] S5.2 `engine/hostload.py` + `schedule.next_due(stretch=)`: an over-limit host's interval checks wait `demand / ceiling` times as long, all alike; fixed-time and manual bookmarks are not stretched; paused bookmarks do not count as demand.
- [x] S5.3 Editor warning on the Schedule tab; Settings > Capacity shows the factor; `HostDemand.stretch`.

## S6 Portal experiment

- [x] S6.1 `logins/experiment.py`: the spec's protocol (probes at 10/20/30/60/120 min after a hand sign-in; two to four spaced password logins a day; challenge detection; pages saved; stop on the first failure), persisted, resumable; findings report as text for DECISIONS.
- [x] S6.2 API `/experiments`, CLI `pagewatch-cli portal-experiment start|list|report|cancel`.
- [ ] S6.3 **Run the experiment on the owner's machine and record the findings in DECISIONS.md.** Not doable here: needs the real portal, the owner's account and 48 hours. Until it is done `stored_password_logins` stays off.

## S7 UI

- [x] S7.1 Editor Login tab (`login_panel.py`), proxy login in Advanced, per-website warning on Schedule.
- [x] S7.2 Macros dialog and Record dialog (`macros.py`).
- [x] S7.3 Main window: *Sign in...* and *Macros* on the toolbar, the daily offer in the status bar; Problems: *Sign in...* / *Resume checks*, sentences for the new reasons.
- [x] S7.4 Settings: stored-password switch, host stretching, sign-in offer, proxy login, Intervals column.

## S8 Tests

- [x] S8.1 Unit: cookies, macro interpreter, login signal, recorder converter, models/migration/routing, static fetch with Basic/Digest/cookies/HTTP and SOCKS5 proxies.
- [x] S8.2 Integration (engine + fixture portal): macro acceptance, stored-password rules, semi-attended acceptance, restart, server auth, cookie import, ACCESS HRA request log, validation, privacy, per-host, prompt, recorder, experiment.
- [x] S8.3 Real Chromium (`-m browser`): the three acceptance criteria, session survival across a closed window, macros / profiles / cookies / Basic / proxy at the fetcher level.
- [x] S8.4 UI: Login tab, proxy login, Macros and Record dialogs, main window, Problems, Settings, editor warning.
- [x] S8.5 Mutation-checked: 15 behaviours broken one at a time, each caught (list in DECISIONS).

## S9 Finish

- [x] S9.1 `docs/ARCHITECTURE.md`, `docs/DECISIONS_M6_LOGINS.md`, `docs/openapi.json`.
- [x] S9.2 `ruff check .` and `ruff format --check src tests tools`, `mypy` (strict), full default suite, and the browser suite, all exit 0 (numbers in DECISIONS).
- [x] S9.3 Commit, push to `claude/m6-logins`, stop (no M7).

## M6 acceptance criteria -> where they are proved

| Criterion (SPEC M6) | Proof |
| --- | --- |
| A macro logs into the fixture login site and detects a change behind it | `tests/integration/test_m6_logins.py::test_a_macro_logs_into_the_fixture_site_and_detects_a_change_behind_it` (HTTP double, real fetcher and interpreter) and `tests/browser/test_m6_chromium.py::...` (real Chromium) |
| The portal experiment is run and its findings recorded before any stored-password login is enabled | **Not run.** The gate exists: `Settings.stored_password_logins` is off and a macro that fills a stored password refuses to run without it (`test_a_stored_password_login_needs_both_...`). The instrument: `tests/integration/test_m6_experiment.py` |
| A semi-attended sign-in checks immediately; an expired session yields exactly one needs-login alert and pauses the bookmark | `test_semi_attended_sign_in_checks_immediately_and_an_expired_session_alerts_exactly_once` (default suite) and `test_a_semi_attended_sign_in_checks_immediately_and_an_expired_session_alerts_once` (real Chromium) |
| The ACCESS HRA E-Notices bookmark never requests a notice PDF, per the request log (**manual check**) | `test_the_e_notices_list_is_watched_and_no_notice_pdf_is_ever_requested` in both suites against the fixture portal. **The check against the real portal is manual and not done.** |

Also covered beyond the criteria: Basic and Digest, rejected credentials pause instead of retrying,
imported cookies and their expiry, proxies (HTTP and SOCKS5, with and without a login), the
6-hour and after-a-failure rules, restart safety of `needs_login`, wrong-password macros that leak
nothing, host stretching and its off switch, the daily offer, recording.

## Manual checks that cannot run in this sandbox

- **The portal experiment** (48 hours, real account): `pagewatch-cli portal-experiment start <id>`.
- **A real `playwright codegen` window**: the converter is tested on JSONL shaped like Playwright's recorder
  output (checked against the installed driver's source); the window itself needs a display.
- **A visible sign-in window** on Windows with Edge: tests use new headless mode on the bundled Chromium.
- **Windows Credential Manager**: tests use an in-memory store (same as the actions branch).
- **ACCESS HRA and Housing Connect themselves**, including their terms of use (the spec asks for that review).
- **An HTTPS proxy** (TLS to the proxy) and CONNECT tunnelling to an https site: no TLS server in the fixtures.

## Merge notes (for combining with `claude/m6-actions`)

Two branches, both from the M-series, built on different bases (`aadd380` vs `90ca022`). Files both touch:

| File | What to do |
| --- | --- |
| `engine/secrets.py`, `tests/support/engine_thread.py`, and the `core.py` default-store line | **Identical** change on both sides; git merges them silently |
| `pyproject.toml`, `uv.lock` | Trivial conflict in the dependency list (this branch adds `keyring` and `httpx[http2,socks]`; that one adds `aiosmtplib` and `keyring`). Keep the union; `uv lock` |
| `models.py` | This branch adds a *Logins* block before `FetchConfig`, `Settings` fields at the end of the class, and API bodies before `LockInfo`. **Duplicate on both sides:** `SECRET_NAME`, `SecretIn`, `SecretOut`. Keep one of each |
| `engine/api/routes_logins.py` vs `routes_actions.py` | **Both define `GET/PUT/DELETE /secrets/{name}`** (same behaviour). Delete one set |
| `engine/api/app.py` | Two `include_router` lines |
| `engine/bookmarks.py` | That branch adds `_default_private_when_signed_in` (create only). This one has the same rule in `logins/validate.py`, covering a later login too (create and patch). Keep this one's, drop that one's |
| `engine/core.py` | Different lines (this branch: `Logins`, `HostLoad`, `notify_problem(kind=)`, scheduler loading of `needs_login`); one possible adjacency near the `Engine(...)` imports |
| `docs/ARCHITECTURE.md` | The secrets row is the same text on both sides; the rest are different sections |
| `docs/openapi.json` | Regenerate after merging: `uv run python tools/gen_openapi.py` |
| `tests/conftest.py` | This branch's `engine` fixture takes a `secret_store` (an in-memory store). That branch did not edit it |
| `DECISIONS_M6.md` / `DECISIONS_M6_LOGINS.md` | Fold both into `DECISIONS.md` as `## M6 · Actions` and `## M6 · Logins and proxies` |

This branch changes `runner.py`, `scheduler`-adjacent code (`schedule.py`, `hostload.py`), `fetch/` and
`repo.py`, which that branch leaves alone; the reverse is also true.
