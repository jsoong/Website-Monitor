"""M6 over the local API: the Problems list, test notifications, write-only secrets, notification
settings, and rejecting bad action parameters at the model boundary."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from pagewatch.engine.secrets import MemorySecrets, NullSecrets, SecretStore
from tests.support.fixture_site import FixtureSite, article
from tests.support.http_sink import HttpSink
from tests.support.sim import advance, settle
from tests.support.smtp_server import SmtpServer


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
def secrets_store(request: pytest.FixtureRequest) -> SecretStore:
    return getattr(request, "param", None) or MemorySecrets()


@pytest.fixture
async def engine(
    data_dir: DataDir,
    clock: FakeClock,
    settings_overrides: dict[str, Any],
    toasts: LogToastBackend,
    secrets_store: SecretStore,
) -> AsyncIterator[Engine]:
    eng = Engine(
        data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
        toast_backend=toasts, rng=random.Random(11), secret_store=secrets_store,
    )  # fmt: skip
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


# -- validation at the API boundary -----------------------------------------------------------


async def test_bad_action_parameters_are_422_with_a_message_naming_the_action(
    client: httpx.AsyncClient, site: FixtureSite
) -> None:
    url = site.url("/x")
    bad = [
        ({"type": "export", "params": {"path": "{nope}.html"}}, "unknown variable {nope}"),
        ({"type": "webhook", "params": {"url": "not a url"}}, "http"),
        ({"type": "email", "params": {"to": ["nobody"]}}, "not an email address"),
        ({"type": "run_program", "params": {"executable": "tool"}}, "explicit confirmation"),
        ({"type": "scrape", "params": {"path": "a.csv", "fields": []}}, "fields"),
        ({"type": "script", "params": {"steps": [{"op": "pause_bookmark"}]}}, "minutes"),
    ]
    for action, message in bad:
        r = await client.post("/bookmarks", json={"url": url, "actions": {"actions": [action]}})
        assert r.status_code == 422, (action, r.text)
        assert action["type"] in r.text and message in r.text, r.text


async def test_valid_action_parameters_are_stored_and_returned(
    client: httpx.AsyncClient, site: FixtureSite
) -> None:
    actions = [
        {"type": "email", "params": {"to": ["me@example.com"], "format": "simple"}},
        {"type": "export", "params": {"path": "{name}/{date:%Y}.html"}},
        {"type": "run_program", "params": {"executable": "tool", "confirmed": True}},
    ]
    r = await client.post("/bookmarks", json={"url": site.url("/x"), "actions": actions})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    got = (await client.get(f"/bookmarks/{bid}")).json()["actions"]["actions"]
    assert [a["type"] for a in got] == ["email", "export", "run_program"]
    assert got[0]["params"] == {"to": ["me@example.com"], "format": "simple"}
    bad = await client.patch(f"/bookmarks/{bid}", json={"actions": {"actions": [{"type": "ntfy",
                                                                          "params": {"topic": "a b"}}]}})  # fmt: skip
    assert bad.status_code == 422 and "topic" in bad.text


async def test_folder_defaults_are_validated_too(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/folders",
        json={
            "name": "F",
            "defaults": {"actions": {"actions": [{"type": "export", "params": {}}]}},
        },
    )
    assert r.status_code == 422 and "export" in r.text


# -- notification settings --------------------------------------------------------------------


async def test_notification_settings_round_trip_and_never_hold_a_password(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    patch = {
        "notifications": {
            "smtp": {"host": "smtp.example.com", "security": "ssl", "port": 465,
                     "username": "me", "from_addr": "me@example.com", "to": ["you@example.com"]},
            "ntfy": {"server": "https://ntfy.example.org", "topic": "alerts-9d3f"},
        }
    }  # fmt: skip
    r = await client.put("/settings", json=patch)
    assert r.status_code == 200, r.text
    smtp = (await client.get("/settings")).json()["notifications"]["smtp"]
    assert smtp["host"] == "smtp.example.com" and smtp["port"] == 465
    assert smtp["password_key"] == "smtp-password" and "password" not in smtp
    stored = await engine.db.read(
        lambda c: [dict(r) for r in c.execute("SELECT key, value_json FROM setting")]
    )
    assert "hunter2" not in repr(stored)
    # a partial update changes only what it names
    await client.put("/settings", json={"notifications": {"smtp": {"port": 25}}})
    smtp = (await client.get("/settings")).json()["notifications"]["smtp"]
    assert smtp["port"] == 25 and smtp["host"] == "smtp.example.com"


@pytest.mark.parametrize(
    "patch",
    [
        {"smtp": {"security": "none-ish"}},
        {"smtp": {"password": "hunter2"}},
        {"ntfy": {"server": "ftp://ntfy"}},
        {"ntfy": {"topic": "bad topic!"}},
    ],
)
async def test_bad_notification_settings_are_422(
    patch: dict[str, Any], client: httpx.AsyncClient
) -> None:
    r = await client.put("/settings", json={"notifications": patch})
    assert r.status_code == 422


# -- secrets ------------------------------------------------------------------------------------


async def test_secrets_are_write_only(
    client: httpx.AsyncClient, secrets_store: SecretStore
) -> None:
    r = await client.get("/secrets/smtp-password")
    assert r.json() == {"name": "smtp-password", "exists": False, "available": True}
    r = await client.put("/secrets/smtp-password", json={"value": "hunter2"})
    assert r.status_code == 200 and r.json()["exists"] is True
    assert "hunter2" not in r.text
    assert secrets_store.get("smtp-password") == "hunter2"
    again = await client.get("/secrets/smtp-password")
    assert again.json()["exists"] is True and "hunter2" not in again.text
    gone = await client.delete("/secrets/smtp-password")
    assert gone.json()["exists"] is False and secrets_store.get("smtp-password") is None


@pytest.mark.parametrize("name", ["has space", "a/b", "x" * 65, "bad!"])
async def test_secret_names_are_restricted(name: str, client: httpx.AsyncClient) -> None:
    r = await client.put(f"/secrets/{name.replace('/', '%2F')}", json={"value": "v"})
    assert r.status_code in (404, 422)


@pytest.mark.parametrize("secrets_store", [NullSecrets()], indirect=True)
async def test_a_machine_without_a_credential_store_says_so(client: httpx.AsyncClient) -> None:
    got = await client.get("/secrets/smtp-password")
    assert got.json() == {"name": "smtp-password", "exists": False, "available": False}
    r = await client.put("/secrets/smtp-password", json={"value": "v"})
    assert r.status_code == 501 and "no secure credential store" in r.text


async def test_the_engine_default_is_a_real_store_or_a_null_one() -> None:
    from pagewatch.engine.secrets import default_secrets

    store = default_secrets()
    assert hasattr(store, "writable") and callable(store.get)


# -- test notifications -------------------------------------------------------------------------


async def test_email_test_message_uses_the_saved_settings(
    client: httpx.AsyncClient, engine: Engine, smtp: SmtpServer
) -> None:
    await engine.update_settings(
        {"notifications": {"smtp": {"host": "127.0.0.1", "port": smtp.port, "security": "none",
                                    "from_addr": "pw@example.com", "to": ["me@example.com"]}}}
    )  # fmt: skip
    r = await client.post("/notifications/test", json={"channel": "email"})
    assert r.json()["ok"] is True, r.text
    [mail] = smtp.messages
    assert mail.message["Subject"] == "[PageWatch] PageWatch test changed"
    assert "test notification from PageWatch" in mail.text()


async def test_email_test_reports_what_is_wrong_in_plain_words(
    client: httpx.AsyncClient, engine: Engine, smtp: SmtpServer
) -> None:
    r = await client.post("/notifications/test", json={"channel": "email"})
    assert r.json() == {"ok": False, "detail": r.json()["detail"]}
    assert "no SMTP server" in r.json()["detail"]
    await engine.update_settings(
        {"notifications": {"smtp": {"host": "127.0.0.1", "port": smtp.port, "security": "none",
                                    "from_addr": "pw@example.com", "to": ["me@example.com"]}}}
    )  # fmt: skip
    smtp.reject = "550 relay denied"
    r = await client.post("/notifications/test", json={"channel": "email"})
    assert r.json()["ok"] is False and "550" in r.json()["detail"]


async def test_ntfy_and_webhook_test_messages(
    client: httpx.AsyncClient, engine: Engine, sink: HttpSink
) -> None:
    await engine.update_settings(
        {"notifications": {"ntfy": {"server": sink.base, "topic": "t-123"}}}
    )
    r = await client.post("/notifications/test", json={"channel": "ntfy"})
    assert r.json()["ok"] is True, r.text
    body = sink.requests[0].json()
    assert body["topic"] == "t-123" and body["title"] == "PageWatch test"
    assert "click" not in body  # the sample has no page to open

    r = await client.post(
        "/notifications/test",
        json={"channel": "webhook", "params": {"url": sink.url("/hook")}},
    )
    assert r.json()["ok"] is True, r.text
    assert sink.requests[1].json()["bookmark"]["name"] == "PageWatch test"

    sink.status = 500
    r = await client.post("/notifications/test", json={"channel": "ntfy"})
    assert r.json()["ok"] is False and "HTTP 500" in r.json()["detail"]
    r = await client.post("/notifications/test", json={"channel": "webhook", "params": {}})
    assert r.json()["ok"] is False and "url" in r.json()["detail"]


async def test_test_notification_rejects_an_unknown_channel(client: httpx.AsyncClient) -> None:
    r = await client.post("/notifications/test", json={"channel": "carrier-pigeon"})
    assert r.status_code == 422


# -- the Problems list --------------------------------------------------------------------------


async def test_action_job_list_filters_by_status_and_retry_dismiss_need_a_failed_job(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    smtp: SmtpServer,
) -> None:  # fmt: skip
    await engine.update_settings(
        {"notifications": {"smtp": {"host": "127.0.0.1", "port": smtp.port, "security": "none",
                                    "from_addr": "pw@example.com", "to": ["me@example.com"]}}}
    )  # fmt: skip
    site.set("/p", article("a"))
    body = {"url": site.url("/p"), "name": "P", "schedule": {"interval_s": 60, "jitter_pct": 0},
            "actions": {"actions": [{"type": "toast"}, {"type": "pushover"}, {"type": "email"}]}}  # fmt: skip
    assert (await client.post("/bookmarks", json=body)).status_code == 201
    await settle(engine, clock)
    site.set("/p", article("a", "b new"))
    await advance(engine, clock, 70)

    everything = (await client.get("/action-jobs", params={"status": "all"})).json()
    assert {j["action_type"]: j["status"] for j in everything} == {
        "toast": "done", "email": "done", "pushover": "failed",
    }  # fmt: skip
    failed = (await client.get("/action-jobs")).json()  # the default is the Problems list
    assert [j["action_type"] for j in failed] == ["pushover"]
    assert failed[0]["bookmark_name"] == "P" and failed[0]["detected_at"]
    assert (await client.get("/action-jobs", params={"status": "done"})).json()
    assert (await client.get("/action-jobs", params={"status": "bogus"})).status_code == 422

    done = next(j for j in everything if j["status"] == "done")
    assert (await client.post(f"/action-jobs/{done['id']}/retry")).status_code == 404
    assert (await client.delete(f"/action-jobs/{done['id']}")).status_code == 404
    assert (await client.post("/action-jobs/9999/retry")).status_code == 404

    gone = await client.delete(f"/action-jobs/{failed[0]['id']}")
    assert gone.status_code == 204
    assert (await client.get("/action-jobs")).json() == []


# -- alert privacy ------------------------------------------------------------------------------


async def privacy_of(client: httpx.AsyncClient, body: dict[str, Any]) -> str:
    r = await client.post("/bookmarks", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["actions"]["alert_privacy"])


async def test_signed_in_bookmarks_default_to_private_alerts(
    client: httpx.AsyncClient, site: FixtureSite
) -> None:
    url = site.url("/x")
    auth = {"auth": {"kind": "basic", "username": "me", "secret_key": "site-pw"}}
    assert await privacy_of(client, {"url": url}) == "content"  # an ordinary page
    assert await privacy_of(client, {"url": url, "fetch": auth}) == "private"
    assert await privacy_of(client, {"url": url, "fetch": {"cookies_file": "c.txt"}}) == "private"
    # asking for the changes explicitly is the owner's call (the editor asks them to confirm)
    explicit = {"actions": [{"type": "toast"}], "alert_privacy": "content"}
    assert await privacy_of(client, {"url": url, "fetch": auth, "actions": explicit}) == "content"
    # actions given as a bare list still get the default
    assert (
        await privacy_of(client, {"url": url, "fetch": auth, "actions": [{"type": "toast"}]})
        == "private"
    )


async def test_a_folder_that_signs_bookmarks_in_makes_them_private_unless_it_says_otherwise(
    client: httpx.AsyncClient, site: FixtureSite
) -> None:
    auth = {"auth": {"kind": "basic", "username": "me", "secret_key": "site-pw"}}
    f1 = (
        await client.post("/folders", json={"name": "Portal", "defaults": {"fetch": auth}})
    ).json()
    assert await privacy_of(client, {"url": site.url("/a"), "folder_id": f1["id"]}) == "private"
    chosen = {"actions": {"actions": [{"type": "toast"}], "alert_privacy": "content"}}
    f2 = (
        await client.post(
            "/folders", json={"name": "Portal 2", "defaults": {"fetch": auth, **chosen}}
        )
    ).json()
    assert await privacy_of(client, {"url": site.url("/b"), "folder_id": f2["id"]}) == "content"
