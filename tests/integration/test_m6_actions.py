"""M6 actions, end to end: a real engine, the fixture web site, an SMTP server and an HTTP sink.

Each test makes a change on the fixture site and looks at what arrived where: exactly one message
per change, the content the privacy setting allows, the retries and the Problems entry when a
channel is down.
"""

from __future__ import annotations

import asyncio
import csv
import json
import random
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.base import AlertContext
from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock, iso
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from pagewatch.engine.secrets import MemorySecrets
from tests.support.docs import page_png
from tests.support.fakes import ScriptedFetcher, ok
from tests.support.fixture_site import FixtureSite, article
from tests.support.http_sink import HttpSink
from tests.support.sim import advance, settle
from tests.support.smtp_server import SmtpServer

OLD = article("Widget costs $19 today", "Gadget in stock", title="Prices")
NEW = article(
    "Widget costs $19 today", "Gadget in stock", "Brand new Doohickey arrives", title="Prices"
)
NEWER = article(
    "Widget costs $19 today", "Gadget in stock", "Brand new Doohickey arrives",
    "Second arrival Thingamajig", title="Prices",
)  # fmt: skip


# -- fixtures -------------------------------------------------------------------------------


@pytest.fixture
async def smtp() -> AsyncIterator[SmtpServer]:
    server = await SmtpServer().start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def sink() -> AsyncIterator[HttpSink]:
    s = await HttpSink().start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.fixture
def secrets_store() -> MemorySecrets:
    return MemorySecrets()


@pytest.fixture
def settings_overrides(smtp: SmtpServer, sink: HttpSink) -> dict[str, Any]:
    return {
        "startup_delay_s": 0.0,
        "per_host_min_gap_s": 0.0,
        "per_host_concurrency": 16,
        "worker_processes": 2,
        "toast_coalesce_s": 0.0,
        "notifications": {
            "smtp": {
                "host": "127.0.0.1", "port": smtp.port, "security": "none",
                "from_addr": "PageWatch <pw@example.com>", "to": ["me@example.com"],
            },
            "ntfy": {"server": sink.base, "topic": "pagewatch-test"},
        },
    }  # fmt: skip


@pytest.fixture
async def engine(
    data_dir: DataDir,
    clock: FakeClock,
    settings_overrides: dict[str, Any],
    toasts: LogToastBackend,
    secrets_store: MemorySecrets,
) -> AsyncIterator[Engine]:
    eng = Engine(
        data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
        toast_backend=toasts, rng=random.Random(7), secret_store=secrets_store,
    )  # fmt: skip
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


# -- helpers --------------------------------------------------------------------------------


async def add(
    client: httpx.AsyncClient, site: FixtureSite, actions: list[dict[str, Any]], *,
    privacy: str = "content", name: str = "Prices", path: str = "/prices", **extra: Any,
) -> int:  # fmt: skip
    if path not in site.pages:
        site.set(path, OLD)  # the first check may run as soon as the bookmark exists
    body = {
        "url": site.url(path), "name": name,
        "schedule": {"interval_s": 60, "jitter_pct": 0},
        "actions": {"actions": actions, "alert_privacy": privacy},
        **extra,
    }  # fmt: skip
    r = await client.post("/bookmarks", json=body)
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def baseline(engine: Engine, clock: FakeClock, site: FixtureSite, page: str = OLD) -> None:
    site.set("/prices", page)
    await settle(engine, clock)


async def change(
    engine: Engine, clock: FakeClock, site: FixtureSite, page: str = NEW, seconds: float = 70
) -> None:
    site.set("/prices", page)
    await advance(engine, clock, seconds)


async def jobs(client: httpx.AsyncClient, status: str = "all") -> list[dict[str, Any]]:
    r = await client.get("/action-jobs", params={"status": status})
    assert r.status_code == 200, r.text
    out: list[dict[str, Any]] = r.json()
    return out


def ctx_for(engine: Engine, **over: Any) -> AlertContext:
    fields: dict[str, Any] = {
        "bookmark_id": 1, "name": "Prices", "url": "https://example.com/p",
        "info": (None, None, None), "change_id": 0, "detected_at": iso(engine.clock.now()),
        "summary": "Brand new Doohickey arrives", "added_words": 3, "removed_words": 0,
        "changed_blocks": 1, "checks_accumulated": 1, "keyword_hits": [], "private": False,
        "params": {},
    }  # fmt: skip
    return AlertContext(**{**fields, **over})


# =============================== email ======================================================


async def test_email_delivers_exactly_one_message_per_change_with_the_changes(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    assert smtp.messages == []  # the first check is a baseline, not an alert
    await change(engine, clock, site)

    assert len(smtp.messages) == 1
    mail = smtp.messages[0]
    msg = mail.message
    assert mail.mail_from == "pw@example.com" and mail.rcpt_to == ["me@example.com"]
    assert msg["Subject"] == "[PageWatch] Prices changed"
    assert msg["Message-ID"].startswith("<pagewatch.") and msg["Message-ID"].endswith(
        "@example.com>"
    )
    assert "Brand new Doohickey arrives" in mail.text()
    html = mail.html()
    assert html is not None and "Brand new Doohickey arrives" in html
    assert "<ins style=" in html and "<style" not in html and "<script" not in html
    assert "Contact us" not in html  # the default format shows the changes only
    # a third check changes nothing: still exactly one message
    await advance(engine, clock, 130)
    assert len(smtp.messages) == 1
    assert [j["status"] for j in await jobs(client)] == ["done"]


async def test_each_new_change_sends_its_own_message(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site, NEW)
    await change(engine, clock, site, NEWER)
    assert len(smtp.messages) == 2
    assert "Second arrival Thingamajig" in smtp.messages[1].text()
    assert "Thingamajig" not in smtp.messages[0].text()
    ids = {m.message["Message-ID"] for m in smtp.messages}
    assert len(ids) == 2  # one Message-ID per change


@pytest.mark.parametrize("fmt", ["simple", "changes_only", "full"])
async def test_email_formats(
    fmt: str, client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    await add(client, site, [{"type": "email", "params": {"format": fmt}}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    mail = smtp.messages[0]
    html, text = mail.html() or "", mail.text()
    assert "Brand new Doohickey arrives" in text and "Brand new Doohickey arrives" in html
    if fmt == "simple":
        assert "Changes:" not in text and "<ins" not in html
    elif fmt == "changes_only":
        assert "Changes:" in text and "<ins" in html and "Contact us" not in html
    else:
        assert "<ins" in html and "Contact us" in html and "Widget costs $19 today" in html
    assert " class=" not in html  # email clients get inline styles only


async def test_email_own_template_and_subject(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    params = {
        "subject": "{{ bookmark.name|upper }}: {{ change.stats.added_words }} new words",
        "template": "{{ bookmark.name }} gained:\n{{ change.added_text }}\n",
        "priority": "high", "to": ["a@example.com", "b@example.com"],
    }  # fmt: skip
    await add(client, site, [{"type": "email", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    mail = smtp.messages[0]
    assert mail.rcpt_to == ["a@example.com", "b@example.com"]
    assert mail.message["Subject"] == "PRICES: 4 new words"
    assert mail.text().strip() == "Prices gained:\nBrand new Doohickey arrives"
    assert mail.html() is None  # a template replaces the built-in HTML body
    assert (mail.message["X-Priority"], mail.message["Importance"]) == ("1", "high")


async def test_private_email_says_which_bookmark_changed_and_nothing_else(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    params = {"format": "full", "attach_screenshot": True,
              "template": "{{ change.added_text }} {{ bookmark.url }} {{ change.summary }}"}  # fmt: skip
    await add(client, site, [{"type": "email", "params": params}], privacy="private")
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert len(smtp.messages) == 1
    raw = smtp.messages[0].data.decode("utf-8", errors="replace")
    for leak in ("Doohickey", "Widget", "127.0.0.1", "/prices", "Contact us"):
        assert leak not in raw
    assert smtp.messages[0].message["Subject"] == "[PageWatch] Prices changed"
    assert "Changed. Open PageWatch to review." in smtp.messages[0].text()
    assert not smtp.messages[0].message.is_multipart() or all(
        p.get_content_type().startswith("text/")
        for p in smtp.messages[0].message.walk()
        if not p.is_multipart()
    )  # no attachment


BASE_SHOT = [(100, 100, 500, 60, "black"), (100, 400, 600, 20, "gray")]
DASH = "<html><body><h1>Dashboard</h1><p>All systems normal.</p></body></html>"


async def screenshot_flow(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, params: dict[str, Any],
    privacy: str,
) -> None:  # fmt: skip
    state = {"png": page_png(BASE_SHOT)}
    shooter = ScriptedFetcher(lambda req, n: ok(req, DASH, png=state["png"]))
    engine.fetchers.replace("screenshot", shooter)  # type: ignore[arg-type]
    body = {
        "url": "https://dash.test/", "name": "Dashboard", "check_method": "screenshot",
        "schedule": {"interval_s": 60, "jitter_pct": 0},
        "actions": {"actions": [{"type": "email", "params": params}], "alert_privacy": privacy},
    }  # fmt: skip
    assert (await client.post("/bookmarks", json=body)).status_code == 201
    await settle(engine, clock)
    state["png"] = page_png([*BASE_SHOT, (800, 600, 200, 100, "red")])
    await advance(engine, clock, 70, step=10)


def attachments(mail: Any) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for part in mail.message.walk():
        if part.get_filename():
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            out.append((part.get_filename(), payload))
    return out


async def test_email_attaches_the_screenshot_diff_when_asked(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, smtp: SmtpServer
) -> None:
    await screenshot_flow(client, engine, clock, {"attach_screenshot": True}, "content")
    [mail] = smtp.messages
    [(name, data)] = attachments(mail)
    assert name == "screenshot-diff.png" and data.startswith(b"\x89PNG")  # the boxed regions
    assert "Visual change" in mail.text()


async def test_email_has_no_attachment_unless_asked_and_never_for_a_private_alert(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, smtp: SmtpServer
) -> None:
    await screenshot_flow(client, engine, clock, {}, "content")
    assert attachments(smtp.messages[0]) == []
    smtp.messages.clear()
    await screenshot_flow(client, engine, clock, {"attach_screenshot": True}, "private")
    assert attachments(smtp.messages[0]) == []


async def test_an_alert_on_the_very_first_check_describes_the_whole_page(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    await engine.update_settings({"notify_on_first_check": True})
    await add(client, site, [{"type": "email", "params": {"format": "full"}}])
    await baseline(engine, clock, site)  # no previous version to compare with
    [mail] = smtp.messages
    assert "Widget costs $19 today" in mail.text() and "Widget costs $19 today" in (
        mail.html() or ""
    )


async def test_email_authenticates_with_the_password_from_the_secret_store(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer, secrets_store: MemorySecrets,
) -> None:  # fmt: skip
    smtp.require_auth = ("pw-user", "s3cret!")
    secrets_store.set("smtp-password", "s3cret!")
    await engine.update_settings({"notifications": {"smtp": {"username": "pw-user"}}})
    await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert len(smtp.messages) == 1 and smtp.messages[0].auth == ("pw-user", "s3cret!")


async def test_a_missing_password_is_a_final_failure_that_names_the_secret(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    await engine.update_settings({"notifications": {"smtp": {"username": "pw-user"}}})
    await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    failed = await jobs(client, "failed")
    assert len(failed) == 1 and failed[0]["attempts"] == 1  # retrying cannot help
    assert "no stored password named 'smtp-password'" in failed[0]["last_error"]
    assert smtp.sessions == 0


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"host": ""}, "no SMTP server"),
        ({"to": []}, "no recipients"),
        ({"from_addr": ""}, "no 'from' address"),
    ],
)
async def test_email_configuration_problems_fail_at_once_with_a_clear_message(
    override: dict[str, Any], message: str, client: httpx.AsyncClient, engine: Engine,
    clock: FakeClock, site: FixtureSite,
) -> None:  # fmt: skip
    await engine.update_settings({"notifications": {"smtp": override}})
    await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    failed = await jobs(client, "failed")
    assert len(failed) == 1 and failed[0]["attempts"] == 1
    assert message in failed[0]["last_error"]


async def test_a_failing_smtp_server_leads_to_retries_then_a_problems_entry_and_retry_works(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    """SPEC M6: 'A failing SMTP server leads to retries, then a Problems entry.'"""
    smtp.reject = "554 mailbox unavailable"
    events = engine.events.subscribe()
    bid = await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)  # first attempt happens now
    assert smtp.sessions == 1
    [job] = await jobs(client, "queued")
    assert job["attempts"] == 1 and "554" in job["last_error"] and job["next_attempt_at"]

    await advance(engine, clock, 75 * 60, step=60)  # 1, 5, 30 and 30 minutes later
    assert smtp.sessions == 5  # five attempts in all, no more
    failed = await jobs(client, "failed")
    assert [(j["action_type"], j["attempts"], j["bookmark_name"]) for j in failed] == [
        ("email", 5, "Prices")
    ]
    assert "554" in failed[0]["last_error"]
    problems = []
    while not events.queue.empty():
        e = events.queue.get_nowait()
        if e.type == "problem":
            problems.append(e.data)
    assert [p["kind"] for p in problems] == ["action_failed"]
    # nothing was lost: the change is still unread because the email never went out
    summary = (await client.get(f"/bookmarks/{bid}")).json()
    assert summary["status"] == "changed"

    smtp.reject = None  # the server is back; the user presses Retry in the Problems list
    r = await client.post(f"/action-jobs/{failed[0]['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["attempts"] == 0
    await settle(engine, clock)
    assert len(smtp.messages) == 1
    assert await jobs(client, "failed") == []


async def test_mail_server_that_drops_the_connection_is_retried_and_then_succeeds(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    smtp.drop_first = 2
    await add(client, site, [{"type": "email"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert smtp.messages == []
    await advance(engine, clock, 10 * 60, step=60)  # retries at +1 and +6 minutes
    assert len(smtp.messages) == 1  # delivered once, on the third attempt
    assert smtp.sessions == 3


# =============================== ntfy =======================================================


async def test_ntfy_publishes_json_with_title_message_and_click(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    params = {"priority": "high", "tags": ["warning", "tada"]}
    await add(client, site, [{"type": "ntfy", "params": params}], name="Prix: café ☕")
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    [req] = sink.requests
    body = req.json()
    assert req.method == "POST" and req.path == "/"
    assert req.headers["Content-Type"].startswith("application/json")
    assert body["topic"] == "pagewatch-test" and body["title"] == "Prix: café ☕"
    assert "Brand new Doohickey arrives" in body["message"]
    assert body["priority"] == 4 and body["tags"] == ["warning", "tada"]
    assert body["click"] == site.url("/prices")
    assert "Authorization" not in req.headers


async def test_ntfy_uses_the_access_token_from_the_secret_store(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink, secrets_store: MemorySecrets,
) -> None:  # fmt: skip
    secrets_store.set("ntfy-token", "tk_abc123")
    await engine.update_settings({"notifications": {"ntfy": {"token_key": "ntfy-token"}}})
    await add(client, site, [{"type": "ntfy"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert sink.requests[0].headers["Authorization"] == "Bearer tk_abc123"


async def test_private_ntfy_alert_sends_no_page_content(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    """SPEC M6: 'private bookmarks send no page content.'"""
    await add(client, site, [{"type": "ntfy"}], privacy="private")
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    [req] = sink.requests
    body = req.json()
    assert body["title"] == "Prices" and body["message"] == "Changed. Open PageWatch to review."
    assert "click" not in body
    raw = req.body.decode()
    for leak in ("Doohickey", "Widget", "127.0.0.1", "prices"):
        assert leak not in raw.replace("pagewatch-test", "")


async def test_ntfy_retries_a_server_error_and_does_not_retry_a_rejected_token(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    sink.statuses = [503]
    await add(client, site, [{"type": "ntfy"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert len(sink.requests) == 1  # 503: transient, queued for a retry
    assert [j["status"] for j in await jobs(client)] == ["queued"]
    await advance(engine, clock, 90, step=30)
    assert len(sink.requests) == 2 and [j["status"] for j in await jobs(client)] == ["done"]

    sink.status = 403  # now the server refuses us: retrying cannot help
    await change(engine, clock, site, NEWER)
    failed = await jobs(client, "failed")
    assert len(failed) == 1 and failed[0]["attempts"] == 1
    assert "HTTP 403" in failed[0]["last_error"]
    assert len(sink.requests) == 3


async def test_ntfy_without_a_topic_fails_with_a_message(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await engine.update_settings({"notifications": {"ntfy": {"topic": ""}}})
    await add(client, site, [{"type": "ntfy"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert "no topic" in (await jobs(client, "failed"))[0]["last_error"]


# =============================== webhook ====================================================


async def test_webhook_posts_the_default_json_payload(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink, secrets_store: MemorySecrets,
) -> None:  # fmt: skip
    secrets_store.set("hook-token", "tok")
    params = {"url": sink.url("/hook?secret=zzz"), "token_key": "hook-token",
              "headers": {"X-Source": "pagewatch"}}  # fmt: skip
    await add(client, site, [{"type": "webhook", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    [req] = sink.requests
    assert req.method == "POST" and req.path == "/hook"
    assert req.headers["Content-Type"].startswith("application/json")
    assert req.headers["Authorization"] == "Bearer tok" and req.headers["X-Source"] == "pagewatch"
    body = req.json()
    assert body["event"] == "change" and body["private"] is False
    assert body["bookmark"]["name"] == "Prices" and body["bookmark"]["url"] == site.url("/prices")
    assert "Brand new Doohickey arrives" in body["change"]["added_text"]
    assert body["change"]["added_words"] == 4


async def test_webhook_body_template_and_private_alerts(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    tpl = '{"text": {{ (bookmark.name ~ ": " ~ change.added_text)|tojson }}, "n": {{ change.id }}}'
    await add(client, site, [{"type": "webhook", "params": {"url": sink.url("/t"), "body": tpl}}])
    await add(
        client, site,
        [{"type": "webhook", "params": {"url": sink.url("/p"), "body": tpl}}],
        privacy="private", name="Secretive", path="/prices2",
    )  # fmt: skip
    await baseline(engine, clock, site)
    site.set("/prices2", NEW)
    await change(engine, clock, site)
    by_path = {r.path: r for r in sink.requests}
    assert by_path["/t"].json()["text"] == "Prices: Brand new Doohickey arrives"
    private = by_path["/p"].json()
    assert private == {"text": "Secretive: ", "n": private["n"]}  # the template sees no content


async def test_webhook_whose_template_is_not_json_fails_at_once(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    params = {"url": sink.url("/t"), "body": "text: {{ change.summary }}"}
    await add(client, site, [{"type": "webhook", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    failed = await jobs(client, "failed")
    assert "not valid JSON" in failed[0]["last_error"] and failed[0]["attempts"] == 1
    assert sink.requests == []


async def test_webhook_errors_never_leak_the_url_secret(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    sink.status = 404
    url = sink.url("/services/T000/B000/XXXX-SECRET-XXXX")
    await add(client, site, [{"type": "webhook", "params": {"url": url}}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    error = (await jobs(client, "failed"))[0]["last_error"]
    assert "HTTP 404" in error and "SECRET" not in error


async def test_webhook_does_not_follow_redirects(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    sink: HttpSink,
) -> None:  # fmt: skip
    site.set("/go", "", status=307, headers={"Location": sink.url("/landed")})
    params = {"url": site.url("/go")}
    await add(client, site, [{"type": "webhook", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert "a redirect" in (await jobs(client, "failed"))[0]["last_error"]
    assert sink.requests == []


# =============================== export =====================================================


async def test_export_html_text_and_diff_json(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    out = tmp_path / "out"
    actions = [
        {"type": "export", "params": {"path": str(out / "{name}" / "c{change_id}.html")}},
        {"type": "export", "params": {"path": str(out / "c{change_id}.txt"), "format": "text"}},
        {"type": "export", "params": {"path": str(out / "c{change_id}.json"), "format": "diff_json"}},
        {"type": "export", "params": {"path": str(out / "plain-{id}.html"), "highlight": False}},
    ]  # fmt: skip
    bid = await add(client, site, actions, name="Rent / Prices: NYC")
    await baseline(engine, clock, site)
    await change(engine, clock, site)

    page = (out / "Rent _ Prices_ NYC" / "c1.html").read_text()  # name made path-safe
    assert '<ins class="pw-add">' in page and "Brand new Doohickey arrives" in page
    assert "Contact us" in page and "<script" not in page
    text = (out / "c1.txt").read_text()
    assert text.startswith("Rent / Prices: NYC\n") and "Brand new Doohickey arrives" in text
    assert any(ln.startswith(("+ ", "~ ")) for ln in text.splitlines())
    data = json.loads((out / "c1.json").read_text())
    assert data["bookmark"]["id"] == bid and data["stats"]["added_words"] == 4
    assert "Brand new Doohickey arrives" in data["added_text"] and data["diff"]["ops"]
    plain = (out / f"plain-{bid}.html").read_text()
    assert "Brand new Doohickey arrives" in plain and "<ins" not in plain
    leftovers = [p for p in out.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []  # atomic writes leave no temp files


async def test_export_relative_paths_land_in_the_data_folder_and_dates_use_the_change_time(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir,
) -> None:  # fmt: skip
    params = {"path": "{name}/{date:%Y-%m-%d}-{datetime}.txt", "format": "text"}
    await add(client, site, [{"type": "export", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    files = sorted((data_dir.root / "exports" / "Prices").iterdir())
    assert len(files) == 1 and files[0].name.startswith("2026-01-05-20260105-")


async def test_export_with_assets_saves_images_and_styles_beside_the_page(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    def page(*items: str) -> str:
        return (
            '<html><head><title>Prices</title><link rel="stylesheet" href="/site.css"></head>'
            '<body><h1>Prices</h1><img src="/logo.png" alt="logo">'
            "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul></body></html>"
        )

    site.set("/site.css", "body { color: #333 }", content_type="text/css")
    site.set("/logo.png", b"\x89PNG-not-really", content_type="image/png")
    out = tmp_path / "archive"
    params = {"path": str(out / "page.html"), "format": "html_assets"}
    await add(client, site, [{"type": "export", "params": params}])
    await baseline(engine, clock, site, page("one"))
    await change(engine, clock, site, page("one", "two"))
    html = (out / "page.html").read_text()
    assert "Prices" in html and "<base" not in html and "Content-Security-Policy" not in html
    assert 'href="page_files/001-site.css"' in html and 'src="page_files/002-logo.png"' in html
    assert (out / "page_files" / "001-site.css").read_text() == "body { color: #333 }"
    assert (out / "page_files" / "002-logo.png").read_bytes() == b"\x89PNG-not-really"
    assert "<ins" in html


async def test_export_to_an_unwritable_path_fails_and_leaves_no_partial_file(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed")
    params = {"path": str(blocker / "sub" / "x.html")}
    await add(client, site, [{"type": "export", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    [job] = await jobs(client, "queued")  # an I/O error is retried like any transient failure
    assert job["attempts"] == 1 and job["last_error"]
    assert blocker.read_text() == "a file where a folder is needed"


# =============================== run_program ================================================

RECORDER = """
import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
rec = {"args": sys.argv[2:]}
for a in sys.argv[2:]:
    p = pathlib.Path(a)
    if p.name in ("new.html", "old.html", "changes.txt") and p.exists():
        rec[p.name] = p.read_text()
        rec["dir"] = str(p.parent)
out.write_text(json.dumps(rec))
"""


def program(script: Path, record: Path, *args: str, **extra: Any) -> dict[str, Any]:
    return {
        "executable": sys.executable,
        "args": [str(script), str(record), *args],
        "confirmed": True, **extra,
    }  # fmt: skip


async def test_run_program_gets_variables_and_files_and_never_a_shell(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path, data_dir: DataDir,
) -> None:  # fmt: skip
    script, record = tmp_path / "rec.py", tmp_path / "record.json"
    script.write_text(RECORDER)
    evil = "Prices; touch PWNED & echo $HOME `id` | cat"
    params = program(
        script, record, "{name}", "{url}", "{file_new}", "{file_old}", "{file_changes}",
        "{info1}", "id={id}", cwd=str(tmp_path),
    )  # fmt: skip
    await add(client, site, [{"type": "run_program", "params": params}], name=evil, info1="x y")
    await baseline(engine, clock, site)
    await change(engine, clock, site)

    rec = json.loads(record.read_text())
    name, url, *_files, info1, ident = rec["args"]
    assert name == evil  # one argument, exactly as written: no shell saw it
    assert url == site.url("/prices") and info1 == "x y" and ident.startswith("id=")
    assert not (tmp_path / "PWNED").exists()
    assert "Brand new Doohickey arrives" in rec["new.html"]
    assert "Brand new Doohickey arrives" not in rec["old.html"]
    assert any("Brand new Doohickey arrives" in ln and ln[:1] in "+~"
               for ln in rec["changes.txt"].splitlines())  # fmt: skip
    assert not await asyncio.to_thread(Path(rec["dir"]).exists)  # removed afterwards
    assert await asyncio.to_thread(list, (data_dir.root / "tmp").iterdir()) == []
    assert [j["status"] for j in await jobs(client)] == ["done"]


async def test_run_program_files_are_only_written_when_an_argument_uses_them(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    script, record = tmp_path / "rec.py", tmp_path / "record.json"
    script.write_text(RECORDER)
    await add(client, site, [{"type": "run_program", "params": program(script, record, "{url}")}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    rec = json.loads(record.read_text())
    assert "new.html" not in rec and "old.html" not in rec


async def test_run_program_non_zero_exit_is_retried_and_reported_with_its_output(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    script = tmp_path / "bad.py"
    script.write_text("import sys; print('boom: disk full'); sys.exit(3)")
    params = {"executable": sys.executable, "args": [str(script)], "confirmed": True}
    await add(client, site, [{"type": "run_program", "params": params}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    [job] = await jobs(client, "queued")
    assert "exited with code 3" in job["last_error"] and "boom: disk full" in job["last_error"]


async def test_run_program_timeout_kills_the_process(engine: Engine, tmp_path: Path) -> None:
    from pagewatch.engine.actions.program import run_program

    marker = tmp_path / "survived"
    script = tmp_path / "slow.py"
    script.write_text(
        f"import time, pathlib; time.sleep(30); pathlib.Path({str(marker)!r}).touch()"
    )
    params = {"executable": sys.executable, "args": [str(script)], "confirmed": True,
              "timeout_s": 1}  # fmt: skip
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="did not finish within 1 s"):
        await run_program(engine, ctx_for(engine, params=params))
    assert asyncio.get_running_loop().time() - started < 10
    await asyncio.sleep(0.2)
    assert not marker.exists()


async def test_run_program_refuses_what_it_cannot_run_safely(
    engine: Engine, tmp_path: Path
) -> None:
    from pagewatch.engine.actions.base import ActionConfigError
    from pagewatch.engine.actions.program import run_program

    with pytest.raises(ActionConfigError, match="was not found"):
        await run_program(
            engine,
            ctx_for(engine, params={"executable": "no-such-program-xyz", "confirmed": True}),
        )
    with pytest.raises(ActionConfigError, match="confirmation"):
        await run_program(engine, ctx_for(engine, params={"executable": sys.executable}))
    with pytest.raises(ActionConfigError, match="unknown variable"):
        await run_program(
            engine,
            ctx_for(engine, params={"executable": sys.executable, "args": ["{nope}"],
                                    "confirmed": True}),
        )  # fmt: skip


# =============================== scrape =====================================================

PRICE_PAGE = (
    '<html><body><h1>Shop</h1><p class="price" data-sku="A1">$19.99</p>'
    '<p class="price" data-sku="B2">$5.00</p><a class="buy" href="/buy/42">Buy</a>'
    "<p>Stock: {stock} units</p></body></html>"
)
FIELDS = [
    {"name": "price", "kind": "css", "expr": "p.price"},
    {"name": "all_prices", "kind": "css", "expr": "p.price", "multiple": True},
    {"name": "sku", "kind": "css", "expr": "p.price", "attr": "data-sku"},
    {"name": "link", "kind": "xpath", "expr": "//a[@class='buy']/@href"},
    {"name": "stock", "kind": "regex", "expr": r"Stock: (\d+) units"},
    {"name": "missing", "kind": "css", "expr": "div.nothing"},
]


@pytest.mark.parametrize("fmt", ["csv", "jsonl", "xml"])
async def test_scrape_appends_one_record_per_change(
    fmt: str, client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    target = tmp_path / f"records.{fmt}"
    params = {"path": str(target), "format": fmt, "fields": FIELDS}
    bid = await add(client, site, [{"type": "scrape", "params": params}], name="Shop, Inc.")
    await baseline(engine, clock, site, PRICE_PAGE.format(stock=10))
    await change(engine, clock, site, PRICE_PAGE.format(stock=7))
    await change(engine, clock, site, PRICE_PAGE.format(stock=3))

    expected = {
        "bookmark_id": bid, "bookmark": "Shop, Inc.", "url": site.url("/prices"),
        "price": "$19.99", "all_prices": "$19.99 | $5.00", "sku": "A1", "link": "/buy/42",
        "missing": "",
    }  # fmt: skip
    if fmt == "csv":
        rows = list(csv.DictReader(target.read_text(encoding="utf-8-sig").splitlines()))
        records: list[dict[str, Any]] = [dict(r) for r in rows]
        assert target.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM: Excel reads UTF-8
        assert list(rows[0]) == ["change_id", "detected_at", "bookmark_id", "bookmark", "url",
                                 *[f["name"] for f in FIELDS]]  # fmt: skip
        for r in records:
            r["bookmark_id"] = int(r["bookmark_id"])
    elif fmt == "jsonl":
        records = [json.loads(ln) for ln in target.read_text().splitlines()]
    else:
        from lxml import etree

        root = etree.parse(str(target)).getroot()
        records = []
        for rec in root.findall("record"):
            d: dict[str, Any] = {c.tag: c.text or "" for c in rec}
            d["change_id"] = rec.get("change_id")
            d["bookmark_id"] = int(d["bookmark_id"])
            records.append(d)
    assert len(records) == 2
    assert [r["stock"] for r in records] == ["7", "3"]
    assert records[0]["change_id"] != records[1]["change_id"]
    for r in records:
        assert {k: r[k] for k in expected} == expected


async def test_scrape_does_not_write_the_same_change_twice(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    from pagewatch.engine.actions.scrape import scrape

    target = tmp_path / "once.csv"
    params = {"path": str(target), "fields": FIELDS[:1]}
    await add(client, site, [{"type": "scrape", "params": params}])
    await baseline(engine, clock, site, PRICE_PAGE.format(stock=10))
    await change(engine, clock, site, PRICE_PAGE.format(stock=7))
    assert len(target.read_text(encoding="utf-8-sig").splitlines()) == 2  # header + 1
    # a crash after the write but before the job was marked done runs the job again
    change_row = (await client.get("/bookmarks/1/changes")).json()["items"][0]
    again = ctx_for(engine, change_id=change_row["id"], bookmark_id=1, params=params)
    await scrape(engine, again)
    assert len(target.read_text(encoding="utf-8-sig").splitlines()) == 2


async def test_scrape_refuses_to_append_under_different_columns(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    target = tmp_path / "cols.csv"
    target.write_text("a,b\n1,2\n")
    await add(
        client, site, [{"type": "scrape", "params": {"path": str(target), "fields": FIELDS[:1]}}]
    )
    await baseline(engine, clock, site, PRICE_PAGE.format(stock=10))
    await change(engine, clock, site, PRICE_PAGE.format(stock=7))
    error = (await jobs(client, "failed"))[0]["last_error"]
    assert "has the columns ['a', 'b']" in error
    assert target.read_text() == "a,b\n1,2\n"


async def test_scrape_csv_does_not_hand_a_spreadsheet_a_formula(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    tmp_path: Path,
) -> None:  # fmt: skip
    target = tmp_path / "safe.csv"
    fields = [{"name": "cell", "kind": "regex", "expr": r"Cell: (.+)"},
              {"name": "num", "kind": "regex", "expr": r"Num: (.+)"}]  # fmt: skip
    await add(client, site, [{"type": "scrape", "params": {"path": str(target), "fields": fields}}])
    await baseline(engine, clock, site, "<p>Cell: x</p><p>Num: 1</p>")
    await change(
        engine, clock, site, '<p>Cell: =HYPERLINK("http://evil","x")</p><p>Num: -12.50</p>'
    )
    row = next(csv.DictReader(target.read_text(encoding="utf-8-sig").splitlines()))
    assert row["cell"].startswith("'=HYPERLINK") and row["num"] == "-12.50"  # numbers stay numbers


# =============================== script =====================================================


async def test_script_checks_a_folder_and_pauses_the_bookmark(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    folder = (await client.post("/folders", json={"name": "Watch Later"})).json()["id"]
    for i in range(2):
        site.set(f"/n{i}", article(f"static {i}"))
        r = await client.post(
            "/bookmarks",
            json={"url": site.url(f"/n{i}"), "folder_id": folder,
                  "schedule": {"interval_s": 3600, "jitter_pct": 0}},
        )  # fmt: skip
        assert r.status_code == 201
    steps = [
        {"op": "check_folder", "name": "watch later"},
        {"op": "pause_bookmark", "minutes": 120},
    ]
    bid = await add(client, site, [{"type": "script", "params": {"steps": steps}}])
    await baseline(engine, clock, site)
    before = {p: len(site.hits_for(p)) for p in ("/n0", "/n1")}
    assert before == {"/n0": 1, "/n1": 1}
    await change(engine, clock, site, seconds=65)  # the alert runs the script
    await settle(engine, clock)  # ...and the checks it queued run
    after = {p: len(site.hits_for(p)) for p in ("/n0", "/n1")}
    assert after == {"/n0": 2, "/n1": 2}  # the folder was checked right away (not in an hour)
    due = (await client.get(f"/bookmarks/{bid}")).json()["next_due_at"]
    assert due > iso(clock.now() + timedelta(minutes=119))  # paused for two hours
    hits = len(site.hits_for("/prices"))
    await advance(engine, clock, 100 * 60, step=300)
    assert len(site.hits_for("/prices")) == hits  # paused for 120 minutes: no checks
    await advance(engine, clock, 30 * 60, step=300)
    assert len(site.hits_for("/prices")) > hits


async def test_script_with_an_unknown_folder_fails_with_its_name(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    steps = [{"op": "check_folder", "name": "No Such Folder"}]
    await add(client, site, [{"type": "script", "params": {"steps": steps}}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert "no folder named 'No Such Folder'" in (await jobs(client, "failed"))[0]["last_error"]


# =============================== order and the rest ========================================


async def test_several_channels_fire_once_each_and_mark_read_last_clears_the_unread_flag(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer, sink: HttpSink, toasts: LogToastBackend,
) -> None:  # fmt: skip
    actions = [{"type": "toast"}, {"type": "email"}, {"type": "ntfy"}, {"type": "mark_read"}]
    await add(client, site, actions)
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    assert len(smtp.messages) == 1 and len(sink.requests) == 1 and len(toasts.shown) == 1
    assert sorted(j["action_type"] for j in await jobs(client)) == [
        "email", "mark_read", "ntfy", "toast",
    ]  # fmt: skip
    assert {j["status"] for j in await jobs(client)} == {"done"}
    unread = (await client.get("/bookmarks", params={"unread": True})).json()["items"]
    assert unread == []


async def test_pushover_stays_unavailable_and_says_so(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await add(client, site, [{"type": "pushover"}])
    await baseline(engine, clock, site)
    await change(engine, clock, site)
    failed = await jobs(client, "failed")
    assert "pushover action is not available" in failed[0]["last_error"]
