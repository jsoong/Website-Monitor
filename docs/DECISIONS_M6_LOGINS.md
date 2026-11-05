# Decisions and deviations: M6 · Logins and proxies

This file is the `## M6 · Logins and proxies` section of `DECISIONS.md`, kept as its own file so it does
not collide with `DECISIONS_M6.md` (the actions half, on `claude/m6-actions`). Fold both in when the
branches are merged. `TASKS_M6_LOGINS.md` has the checklist, the acceptance table and the merge notes.

Branch `claude/m6-logins`, built from `aadd380` (M0–M5 including M5 part 2).

## What this is, and what it is not

**Built:** everything the actions branch listed as unbuilt (macros, semi-attended sign-in, cookie
import, server auth, per-bookmark proxies, per-host overrides, the portal experiment's tooling) plus
the rules the spec attaches to them.

**Not done, and not claimed:**

* **The portal experiment has not been run.** It is a 48-hour test on the owner's machine against the
  real ACCESS HRA and Housing Connect sign-ins, with the owner's accounts. Nothing in this session can
  do that, so **no findings exist and none are recorded below**. What exists is the instrument (next
  section), tested against a fixture portal on a fake clock, and a gate: `Settings.stored_password_logins`
  is off by default and a macro that fills a stored password refuses to run without it.
* **A real `playwright codegen` window** was not opened (no display here). The converter is tested on
  JSONL shaped like the recorder's output, taken from the installed driver (Playwright 1.63) source.
* **A visible sign-in window** was not seen. The sign-in code path is the same with `headless=False`;
  tests run it in new headless mode.
* **ACCESS HRA and Housing Connect themselves**, and their terms of use, which the spec says to review
  before any automated check.

## The portal experiment

### What the instrument does (`engine/logins/experiment.py`)

The spec's protocol, step for step:

1. `pagewatch-cli portal-experiment start <bookmark>` (or `POST /experiments`) on a bookmark that has
   semi-attended sign-in, a macro, and a **manual** schedule (the engine refuses otherwise: any other
   check would refresh the session and spoil the measurement).
2. The owner signs in by hand with the normal *Sign in...* window. That moment is time zero.
3. **Probes** at 10, 20, 30, 60 and 120 minutes (configurable) open the bookmark's page on the profile
   *without logging in* and record whether the session is still there.
4. **Password logins**, `logins_per_day` of them (2 to 4, default 3), evenly spaced, starting 30 minutes
   after the last probe so a login cannot spoil it, until `duration_h` (default 48) has passed. Each runs
   the bookmark's macro on a **fresh empty profile**, so a real password login happens, and the page it
   ends on is checked for a one-time-code or CAPTCHA (selectors and wording), saved to
   `experiments/<id>-login-<n>.html` when it did not get through.
5. The first failed or challenged login **stops the attempts** (spec: none after a failure).
6. `pagewatch-cli portal-experiment report <id>` prints the findings as the text below, for pasting here.

State is kept in `experiments/<id>.json` and survives an engine restart.

### Reading the result: a caveat the report states itself

Each probe is itself activity, so a session that survives probes 10 minutes apart says little about a
30-minute idle timeout. The report therefore words an expiry as "an idle timeout shorter than the gap
**or** an absolute timeout between the two probes" and says so; to separate the two, repeat the run
with longer gaps (`--probes`). The verdict `looks_safe` needs at least four password logins, all
successful, none challenged; any failure or challenge is `not_safe`; fewer is `not_enough_data`.
These thresholds are mine, not the spec's, which asks only for "two to four spaced password logins a day".

### Findings (to be filled in by whoever runs it)

> **NOT RUN.** Do not enable stored-password login for ACCESS HRA or Housing Connect until this
> table is filled in from `portal-experiment report` and the verdict is `looks_safe`.

| Portal | Date run | Idle timeout / absolute timeout | Code or CAPTCHA on routine logins | Challenge pages saved | Verdict |
| --- | --- | --- | --- | --- | --- |
| ACCESS HRA | not run | | | | |
| NYC Housing Connect | not run | | | | |

Then turn on **Settings > Network > Allow stored-password logins**, and tick **Let the macro sign in with
a stored password** on each bookmark that should use it.

### What the instrument was proved to do

On the fixture portal, with a fake clock (`tests/integration/test_m6_experiment.py`): probes happen at
the set minutes and never log in; a 45-minute idle timeout is reported as alive at 10/20/30/60 and gone at
120 (the gaps being 10, 10, 10, 30 and 60 minutes); six logins happen at 8-hour spacing over 48 hours;
a one-time-code challenge stops the attempts after one, with the page saved and the portal contacted
no further; the report reads sensibly before, during and after; it survives a restart; it refuses to
start on a bookmark that would spoil it; 12 challenge/non-challenge pages are classified correctly.

## Scope and base

* **The brief and the checkout disagreed on the base commit.** The task said to start from
  `claude/focused-mccarthy-2a74te (aadd380)`. The branch of that name (local and on `origin`) is at
  `a051864`; `aadd380` ("M5 part 2: Settings and Problems screens...") is its child, present in the
  object store but on no branch. I based `claude/m6-logins` on **`aadd380`**, as named: it is the
  commit whose Settings window and Problems list this work extends. `a051864` is its parent, so
  nothing is lost either way. `aadd380` was therefore not on any remote branch before this push.
* **`claude/m6-actions` is not merged.** The task said to inspect it. It was built from `90ca022`
  (before M5), so merging means resolving `models.py`, `core.py` and others against M5. Where the two
  halves must share something (the secret store, private-by-default) this branch uses the actions
  branch's `engine/secrets.py` **byte for byte**, so git merges that file silently; the rest is
  listed in the merge notes.
* **"Per-host overrides" was partly done already.** `Settings.host_overrides`, `HostGate` honouring
  them, the Settings table and the capacity page came with M5 part 2. Verified by tests, not redone.
  What M5 part 2 explicitly left out is built here: the scheduler stretching an over-limit host's
  intervals and the editor's warning, plus normalising the override keys. If "per-host overrides"
  was meant to include per-host proxy or user agent, that is not built (the spec's only per-host item is
  the politeness limit).

## Schema, dependencies, API

The working agreement asks before changing the schema outside a migration, adding a dependency the spec
does not name, or changing a public API shape. This session ran unattended; the choices and how to undo them:

* **Migration `0002_logins.sql`** adds one table, `login_state`. It is a migration (backup first, one
  transaction), additive, and nothing existing changes. Alternative if unwanted: the same facts in the
  `setting` table under `_state.` keys, as M5 did, at the cost of cleaning up after deleted bookmarks by hand.
* **Dependencies:** `keyring` (the spec's Secrets row names it) and httpx's `socks` extra (`socksio`;
  the spec requires SOCKS5 proxies and httpx cannot do them without it).
  `aiosmtplib`, which the actions branch adds, is not needed here.
* **API additions** (all additive; `docs/openapi.json` regenerated):
  `GET/POST /macros`, `GET/PUT/DELETE /macros/{id}`, `POST /macros/record`, `GET/DELETE /macros/record/{id}`;
  `GET /bookmarks/{id}/login`, `POST .../login/resume`, `POST .../signin`, `POST .../signin/finish`,
  `DELETE .../session`, `GET/POST/DELETE .../cookies`; `GET/POST /experiments`, `GET/DELETE /experiments/{id}`,
  `GET /experiments/{id}/report`; `GET/PUT/DELETE /secrets/{name}` (identical to the actions branch's).
  `FetchConfig` gains `login`, `proxy_username`, `proxy_secret_key`; `Settings` gains
  `global_proxy_username`, `global_proxy_secret_key`, `stored_password_logins`, `signin_prompt_enabled`,
  `signin_prompt_time`, `stretch_hosts`, `host_load_refresh_s`; `HostDemand` gains `stretch`;
  `EVENT_TYPES` gains `signin`. `PUT /settings` now normalises `host_overrides` keys.

## Decisions

### Sessions

* **Chromium discards session cookies when a persistent profile closes**, and many portals keep their
  login in exactly such a cookie. The first real-browser test of "the second check reuses the session"
  failed for this reason. Fix: when a profile's browser closes, its cookies (session ones included) are
  written next to it (`pagewatch-session.json`, owner-only) and put back at the next launch. The sign-in
  window copies cookies out every second and when a page closes, because a window the owner closes takes
  its cookies with it. At most about a second of cookies is at risk. This is the one place where the
  design in the spec ("a persistent context in `profiles\<id>\`") was not enough by itself.
* **A persistent context is its own browser process** (Playwright cannot make one inside a running
  browser), opened for a check and closed after it. Costs about a second per signed-in check; the 3-page
  pool still bounds it. Checks that only need cookies, a proxy or a server login use a fresh context in
  the shared browser instead.
* **While the owner's sign-in window is open, scheduled checks of that bookmark are skipped**
  (`check_run` outcome `skipped`, reason `signing_in`, retried in 30 s), because a Chromium profile is
  held by one process.
* **Static checks keep a client and cookie jar per signed-in bookmark**, so sessions of two bookmarks on
  the same site never mix. A server-set cookie lives in memory until restart; it is not written back to
  the imported file. For a session that rotates, use the browser path.
* **A wiped session** (`DELETE .../session`) removes the profile and the in-memory jar; it refuses while
  the sign-in window is open.

### Expiry, alerts, pausing

* **One rule for every kind of login:** a login signal on the page, or HTTP 401 for a bookmark that sent
  credentials or cookies, moves it to `needs_login`. A 401 with credentials is treated this way rather
  than as an error because retrying wrong credentials is how accounts get locked.
* **Alert exactly once:** only the check that moves a bookmark *into* `needs_login` alerts. Manual checks of
  a paused bookmark stay silent. After `resume` the next failure is a new expiry and alerts again.
* **Paused means:** `next_due_at` is NULL, the scheduler holds no due time, and the startup loader keeps
  it that way (otherwise a NULL due time means "due now"). Demand and the capacity page ignore it.
  Manual "Check now" still works.
* **Recorded as** a `check_run` with outcome `error` and reason `login_signal` / `auth_rejected` /
  `macro_failed` / `stored_password_off` / `login_rate_limited` / `login_blocked`, which is what the
  existing Problems query already reads. It does **not** touch `consecutive_errors` or send the
  "is failing" alert. The longer explanation (e.g. which macro step failed) is in `login_state.reason`.
* **Leaving it:** a good check, a sign-in, importing cookies, or `POST .../login/resume`.

### The stored-password rules (spec: Portal strategy)

* "A macro with a `{"secret": ...}` fill" is what counts as a stored-password login. A macro without one
  (a consent wall, a public code) is not gated, because the rules exist to protect the account from
  repeated password logins.
* **Two switches, both off by default:** the global `stored_password_logins` (to be turned on only after
  the experiment) and the bookmark's `allow_stored_password`.
* **At most one automated login per `min_login_interval_s`** (default and spec figure 6 h; I set the
  floor at 1 h so it cannot be set to something meaningless). The attempt is written *before* the macro
  runs, so a crash cannot be followed by an immediate second try.
* **None after a failure** until `POST .../login/resume` (the *Resume checks* button).
  **`resume` lifts the failure block but not the 6-hour limit**: the failed attempt counts as that
  window's one login. The consequence is that after fixing a wrong password the owner may wait up to
  six hours before the macro can run again (the status shows when, `next_auto_login_at`). That is the
  literal reading of "at most one per 6 hours, and none after a failure"; the alternative (resume
  resets the clock) would let one button defeat the rule. Easy to change if the owner disagrees.
* A macro's failure message is built from the *step* ("step 3 (fill #password) timed out"), never from
  Playwright's exception, whose call log can repeat the typed value. A test makes the exception carry the
  password and checks it cannot reach the error's message or text, and an engine test checks the wrong
  password cannot reach the stored status. (The log lines were not checked separately.)

### Secrets and privacy

* **Passwords go through `PUT /secrets/{name}` once and are never returned.** The database holds key names
  only. Every password field in the UI empties itself the moment it sends. A password field in a macro
  step cannot hold an inline value: the model refuses it (a selector containing `password`, `passwd`,
  `passcode`, `pwd` or `secret`). That is a heuristic; a false positive only means using a secret.
* **Recording drops typed passwords** and writes `{"secret": "<key>"}`, with a warning to store the
  password under that name. Selectors are kept as Playwright recorded them, including `internal:`
  locators, which work with the pinned Playwright but are not a documented, stable syntax.
* **Signed-in bookmarks default to `alert_privacy: private`** when created and when a login is added
  later, unless the bookmark or a folder it inherits from chose. (The actions branch applies the default
  at creation only; its editor also asks for confirmation when switching back to `content`, which is its
  half to build.)
* **Imported cookies are copied into `cookies/<id>.txt`, owner-only,** and never included in a backup or
  export (the spec says secrets are not). After restoring on another machine they must be imported again.
* **Basic auth over plain http to a non-local host is refused.** Loopback and private addresses are
  allowed. (The spec does not say; the actions branch refuses cleartext SMTP passwords the same way.)
* **A proxy address cannot contain a password** (or a user name); a proxy login is a user name plus a
  secret-store key. Playwright documents that Chromium does not authenticate to a SOCKS5 proxy, so a browser check
  with a SOCKS5 login fails with a message saying so (not exercised here); static checks can, and that is tested.

### Per-host

* Stretch factor = `demand / ceiling` per host (demand as the Capacity page measures it, ceiling =
  `60 / minimum gap` from the host's own limits). It multiplies the *wait* for interval and adaptive
  schedules when the next due time is computed; the stored adaptive interval is untouched so the factor
  cannot feed back into the demand it came from. Fixed times and manual schedules are never stretched;
  days / window limits are still applied after. Re-measured every `host_load_refresh_s` (5 min) and on
  any settings change that affects it. `stretch_hosts` turns it off.
* Override keys are normalised in the model, in `PUT /settings` (so `{"EXAMPLE.com.": null}` removes
  `example.com`) and in the Settings table (a pasted address is reduced to its host name). Without the
  last one, retyping a host as an address produced an "add and remove" pair that normalised to the same
  key and removed it.

### Smaller choices

* The daily sign-in offer is a status-bar line, a toolbar count and one toast; never a dialog that steals
  focus. Default 09:00 local, "or at the next wake", once a day.
* In bulk edit the Login tab shows only a note: sign-in belongs to one bookmark.
* `ruff format --check .` reports `docs/SPEC.md` (a Python snippet inside the spec) as unformatted with this
  ruff. That is the spec, not code, and was true before this session; the format gate used here is
  `src tests tools`. Tests may declare `timeout=` parameters on async test doubles (rule `ASYNC109` is off
  for `tests/**`) because the doubles mirror Playwright's signatures.

## Verified, and how

| Claim | Evidence |
| --- | --- |
| A macro logs in through a real browser, and the second check reuses the session | `tests/browser/test_logins_chromium.py`, `tests/browser/test_m6_chromium.py` (Chromium 1243) |
| The same flows through the engine without a browser | `tests/integration/test_m6_logins.py` (the real `BrowserFetcher`, interpreter and plan over an HTTP-backed page against the fixture portal) |
| Expiry alerts once, pauses, survives a restart | `test_semi_attended_sign_in_checks_immediately_...`, `test_needs_login_survives_a_restart_...` |
| HTTP and SOCKS5 proxies, with and without a login; Basic, Digest, cookies | `tests/unit/test_login_fetch.py` (in-process HTTP proxy and SOCKS5 server on virtual host names, so a page arriving proves the proxy was used) |
| Browser traffic goes through a per-bookmark proxy | `test_a_bookmark_proxy_carries_browser_traffic` |
| Host stretching | `tests/integration/test_m6_hosts.py` |
| UI | `tests/ui/test_logins_ui.py` |

**Mutation checks.** Fifteen behaviours were broken one at a time and the guarding tests run; all
fifteen were caught: alerting on every needs-login check; not pausing after it (the DB `next_due_at`
was only caught after I added an assertion for it); the stored-password gate; the 6-hour limit; the
block after a failure; not restoring session cookies; not stretching; a macro error repeating
Playwright's text; no private default; host-override removal by another spelling; Basic over plain
http; the experiment continuing after a challenge; keeping a recorded typed password; the daily offer
firing twice; a deleted bookmark keeping its saved sessions. One was first reported as surviving only because I had selected a single test with `-k`
(the model's own "no inline password" rule had dropped the step instead); the whole file catches it.

## Bugs found by writing the tests

* Session cookies lost when a persistent profile closes (above), found by the first real-browser reuse test.
* A fake browser that kept cookies in memory after its profile folder was deleted made the experiment's
  "fresh profile" login look like a success; the double now starts empty when its folder is gone.
* Host overrides retyped as addresses silently deleted themselves (above).
* Deleting a bookmark left its browser profile (live session cookies) and imported cookie file on disk. Found
  in review, not by a test; `Logins.forget` now removes both, closes an open sign-in window, and drops the
  in-memory jar, for both delete routes. A test covers it and the fix was mutation-checked.
* A circular import between the experiment and the service module; the pure helpers moved to
  `logins/common.py`.
* `cssselect` has no case-insensitive attribute flag, so the one-time-code selector failed to parse and
  every OTP page was classified as no challenge; the broad `except` hid it until a test asked.

## A timing test that failed, and what it really was

`tests/ui/test_scale.py::test_ten_thousand_bookmarks_scroll_sort_and_filter_in_under_200ms` has a hard
200 ms budget. In the first two full runs it failed (worst scroll page 209 ms and 224 ms) while passing alone
at 92–139 ms. My first explanation, "the machine was busy", was wrong for the second run, which had the
machine to itself. Measured instead, running the whole `tests/ui` sequence in one process:

| | worst scroll page |
| --- | --- |
| base commit `aadd380` | 131 ms |
| this branch, before the fix | 170 ms (209–224 ms in the full suite) |
| this branch, after the fix | 132 ms and 139 ms |

The cause was in my tests, not the product: the 27 new UI tests built dialogs (editors, Settings, Macros,
Problems) and never closed them, so they piled up as live top-level widgets in the one shared
`QApplication`, and every later test, this one included, paid for them. The fix is an autouse fixture in
`test_logins_ui.py` that closes and deletes what each test opened after the engine has stopped. The
engine-side suite was not the cause (base 142 ms vs 133 ms after the whole integration suite). Worth
knowing for whoever writes the next UI test: the shared `QApplication` makes leaked widgets everyone's problem.

## Tests

Default suite: 956 -> **1116 passed** (+160), 1 skipped as before; `-m browser`: 12 -> **24 passed** (+12). Both exit 0.
New files: unit `test_cookies`, `test_login_signal`, `test_macro_runner`, `test_macro_recorder`,
`test_login_fetch`, `test_logins_models`; integration `test_m6_logins`, `test_m6_hosts`,
`test_m6_prompt_recorder`, `test_m6_experiment`; UI `test_logins_ui`; browser `test_logins_chromium`,
`test_m6_chromium`. Support: `login_site` (the fixture portal), `http_browser`, `proxy_servers`, `fetching`.
`ruff check .`, `ruff format --check src tests tools` and `mypy` (strict, 90 files) exit 0, and the committed `docs/openapi.json` is what `tools/gen_openapi.py` produces.

## Known limits

* One recording at a time; it is limited to 30 minutes and cleaned up with the engine.
* A signed-in browser check opens a browser process each time; at hundreds of signed-in bookmarks on short
  intervals the browser pool (3) is the limit, as for any browser check.
* The macro's `assert_text` and the login-signal text match is a case-insensitive substring.
* The experiment's idle-timeout reading is bounded by the confound described above.
* Digest and Basic over HTTP are tested against the fixture; NTLM / Kerberos are not supported (the spec
  names Basic and Digest).
