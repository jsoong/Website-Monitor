# TASKS: Actions domain (milestone M6)

Branch `claude/m6-actions`, started from 90ca022 (M0-M4, 791 tests). A parallel session builds M5
(unattended operation and scheduling) from the same commit, so this file replaces nothing in
`TASKS.md`, and the work is kept in new modules wherever it can be (see *Merge notes* below).

## Scope decision (read this first)

SPEC.md names M6 **"Actions and logins"**. The request for this session described M6 as the
actions half: notifications, alert delivery channels, webhooks, executing actions when a change
passes the gate, and the UI wiring for them. This session implements exactly that half.

| SPEC M6 item | This session |
| --- | --- |
| Email, ntfy, export, run program, scrape and script actions | **Done** |
| Webhook action (SPEC: "post-v1"; the request names webhooks explicitly) | **Done**, see DECISIONS_M6 |
| Pushover (SPEC: post-v1; not requested) | Not built; stays "not available" |
| Keyring-backed secret store (SMTP password, ntfy token, FTP password) | **Done** (actions need it) |
| Failed jobs in the UI's Problems list; retries then a Problems entry | **Done** (API + dialog) |
| Alert privacy (`private` sends no page content) | **Done** |
| Optional semi-attended sign-in, portal experiment | **Not done** (logins half) |
| Check-Macro recorder and replay | **Not done** (logins half) |
| Cookie import, Basic/Digest auth in the fetchers, per-bookmark proxies | **Not done** (logins half) |
| Per-host overrides in the scheduler | **Not done** (logins half; the `Settings.host_overrides` model already exists) |

The logins half needs the fetcher, runner and scheduler (`needs_login` pauses a bookmark), which is
where the M5 session works. It is the natural second M6 session once M5 is merged.

## Audit: SPEC vs repository at 90ca022

| SPEC requirement | State in the repo | Task |
| --- | --- | --- |
| `action_job` durable queue, retries at 1/5/30 min, 5 attempts, `problem` event | Done (`actions/queue.py`) | reuse unchanged |
| `toast`, `sound`, `open`, `mark_read` | Done (`actions/builtin.py`, `toast.py`) | reuse |
| `email`, `export`, `run_program`, `ntfy`, `scrape`, `script`, `webhook` | Registered as `_unavailable(...)` | S3 |
| `ActionConfig.params` | Free `dict`, no validation | S1.2 |
| SMTP / ntfy server settings | Absent | S1.3 |
| `SecretStore` | Interface plus `NullSecrets`/`MemorySecrets`; no keyring | S2.5 |
| Jinja2 sandbox templates; `{var}` path/argument templates | Absent | S2.1 |
| Alert text for a change (added text, changes-only HTML) | Only the 200-char `summary` | S2.2, S2.3 |
| Alert privacy | `AlertContext.body` for toasts only | S3 (all channels) |
| Problems list for failed action jobs | `problem` event only; no way to list or retry | S4.1 |
| Editor Actions tab | Check boxes; `email` etc. labelled "not available", no parameters | S5.1 |
| Settings screen for email/ntfy | Absent | S5.2 |

## S0 Setup

- [x] S0.1 Branch `claude/m6-actions` from 90ca022.
- [x] S0.2 Dependencies named by the spec: `aiosmtplib`, `keyring` (`uv add`).
- [x] S0.3 Baseline: 791 passed + 1 skipped at 90ca022.

## S1 Models and settings (`models.py`)

- [x] S1.1 Parameter models per action (`EmailParams`, `NtfyParams`, `WebhookParams`, `ExportParams`, `RunProgramParams`, `ScrapeParams`, `ScriptParams`) with template syntax checks.
- [x] S1.2 `ActionConfig` validates `params` for those types (HTTP 422 at create/patch); `run_program` needs `confirmed: true`.
- [x] S1.3 `Settings.notifications` (`SmtpSettings`, `NtfySettings`); API bodies for action jobs, test notifications and secrets.
- [x] S1.4 Regenerate `docs/openapi.json`.

## S2 Shared action plumbing (`engine/actions/`)

- [x] S2.1 `templating.py`: `{var}` expander (path/argument templates, `{date:<fmt>}`, safe file names) and the sandboxed Jinja2 environment.
- [x] S2.2 `content.py`: worker job that builds a change's alert content from blobs (added/removed text, plain diff, email-safe changes HTML, optional full highlighted page).
- [x] S2.3 `htmlmail.py`: email-safe HTML (inline styles only, table layout, no scripts/remote loads).
- [x] S2.4 `runtime.py`: per-engine content cache; `ActionConfigError` (final failure, no retries); privacy gate.
- [x] S2.5 `secrets.py`: `KeyringSecrets` (Windows Credential Manager) with `set`/`delete`; default store chosen by the engine entry point.

## S3 Actions

- [x] S3.1 `email` (`mailer.py`): SMTP over TLS via aiosmtplib, formats full/simple/changes_only, screenshot attachment, template, priority, one message per change (stable Message-ID).
- [x] S3.2 `ntfy`: server, topic, token from the secret store, priority; JSON publishing; private alerts carry no content.
- [x] S3.3 `webhook`: URL, method, Jinja2 JSON body, headers, optional bearer token.
- [x] S3.4 `export`: path template, `html` / `html_assets` / `text` / `diff_json`, atomic write.
- [x] S3.5 `run_program`: argv (no shell), `{var}` templates, files for new/old/changes, timeout kills, output logged, explicit confirmation.
- [x] S3.6 `scrape`: named fields (CSS, XPath, regex), one record per change appended to CSV / JSON Lines / XML; idempotent per change.
- [x] S3.7 `script`: declarative `check_folder(name)` and `pause_bookmark(minutes)`.
- [x] S3.8 `builtin.py` registers them; `webhook` becomes available, `pushover` stays unavailable.

## S4 Engine API

- [x] S4.1 `GET /action-jobs`, `POST /action-jobs/{id}/retry`, `DELETE /action-jobs/{id}` (the Problems list).
- [x] S4.2 `POST /notifications/test` (send a test email/ntfy/webhook with the saved settings).
- [x] S4.3 `GET/PUT/DELETE /secrets/{name}` (write-only values; the database keeps key names only).
- [x] S4.4 Signed-in bookmarks (auth/cookies/macro) default to `alert_privacy: private`.

## S5 UI wiring

- [x] S5.1 Editor Actions tab: per-action parameters dialog, implemented actions enabled, `run_program` confirmation, content-privacy confirmation for signed-in bookmarks.
- [x] S5.2 Notifications dialog (SMTP + ntfy, passwords to the secret store, send-test buttons).
- [x] S5.3 Failed actions dialog (Problems) with retry/dismiss; main-window menu entries; status-bar message on a failed action.
- [x] S5.4 `ApiClient` support.

## S6 Tests

- [x] S6.1 Unit: param models, templating, content builder, email HTML, mailer (fake SMTP server), ntfy and webhook (HTTP server), export, program, scrape, script, secrets.
- [x] S6.2 Integration (engine + fixture site + fake SMTP/HTTP servers): each channel delivers exactly once per change; private bookmarks send no content; failing SMTP retries then a Problems entry; retry endpoint; test-notification endpoint; secrets endpoints.
- [x] S6.3 UI: action parameters dialog, notifications dialog, failed-actions dialog, editor round trip.

## S7 Finish

- [x] S7.1 `docs/ARCHITECTURE.md`, `docs/DECISIONS_M6.md`, `docs/openapi.json`.
- [x] S7.2 ruff (check and format), mypy `--strict`, schema check, full suite green: **1016 passed + 1 skipped** (default; was 791 + 1), 12 passed (`-m browser`). pytest exit code 0.
- [ ] S7.3 Commit, push to `claude/m6-actions`, stop (no M7).

## M6 acceptance criteria → where they are proved

| Criterion (SPEC M6) | State |
| --- | --- |
| A failing SMTP server leads to retries, then a Problems entry | `test_m6_actions.py::test_a_failing_smtp_server_leads_to_retries_then_a_problems_entry_and_retry_works` (5 attempts at 1, 5, 30, 30 minutes, the `problem` event, `GET /action-jobs`, then Retry delivers); the list, retry and dismiss endpoints in `test_m6_api.py`; the dialog in `test_action_dialogs.py` |
| Private bookmarks send no page content | email, ntfy and webhook each have a test that searches everything sent for the page text, URL and summary (`test_private_email_...`, `test_private_ntfy_alert_...`, `test_webhook_body_template_and_private_alerts`); `test_action_content.py` for the template variables |
| A changes-only email renders correctly in Gmail and Outlook.com | **Manual check.** Tested: inline styles only, no `<style>`/classes/scripts/remote loads, table layout |
| An ntfy alert reaches the phone | **Manual check.** The request is tested against a local server |
| A macro logs into the fixture login site and detects a change behind it | **Not built** (logins half) |
| The portal experiment is run and recorded in DECISIONS.md | **Not built**; it is a 48-hour experiment on the owner's machine |
| Semi-attended sign-in checks immediately; an expired session yields one needs-login alert and pauses the bookmark | **Not built** (logins half) |
| The ACCESS HRA E-Notices bookmark never requests a notice PDF | **Not built** (needs the logins half and a manual check) |

Also covered beyond the criteria: each export format, programs without a shell and with a timeout,
scrape in three formats and once per change, `script`, screenshot attachments, alerts on a first
check, TLS arguments, the refusal to send a password in the clear, the secret store, the editor,
the Notifications dialog and the Problems dialog.

## Merge notes (for combining with the M5 branch)

M5 touched `models.py`, `core.py`, `settings.py`, `actions/queue.py`, `runner.py`, `scheduler.py`,
`main.py`, `cli/main.py`, `docs/*`, `TASKS.md`. M6 keeps clear of the same lines:

- `actions/queue.py`, `runner.py`, `scheduler.py`, `settings.py` are **not edited**.
- `models.py`: new classes in the Actions block; `Settings` gets one field inserted next to
  `toast_coalesce_s` (M5 appended its fields after `autowatch_paused_until`).
- `core.py` and `main.py`: one line each.
- `DECISIONS_M6.md` is a separate file to be folded into `DECISIONS.md` as its `## M6` section.
- `docs/openapi.json`: regenerate after merging (`uv run python tools/gen_openapi.py`).

## Manual checks that cannot run in this sandbox

- A changes-only email in Gmail and Outlook.com (rendering).
- An ntfy alert reaching a phone (needs the real service).
- Real STARTTLS/implicit-TLS SMTP handshakes (the in-process server speaks plain SMTP; the TLS
  arguments are asserted on the aiosmtplib call).
- Windows Credential Manager (the tests use an in-memory keyring backend).
