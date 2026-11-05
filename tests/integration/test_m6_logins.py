"""M6 logins, end to end through the engine: macros, the stored-password rules, semi-attended
sign-in, session expiry, server auth, imported cookies, privacy, and the ACCESS HRA rule.

The browser is ``HttpBrowser`` (see tests/support/http_browser.py): the *real* BrowserFetcher,
macro interpreter and login plan run against the real fixture portal over HTTP. The same flows
run in a real Chromium in tests/browser/test_m6_chromium.py.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.fetch.browser import BrowserFetcher
from pagewatch.engine.secrets import MemorySecrets
from tests.support.http_browser import HttpBrowser
from tests.support.login_site import LoginSite
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}
SIGNAL = {"selector": "input[type=password]"}


def macro_body(site: LoginSite, name: str = "portal") -> dict[str, Any]:
    return {
        "name": name,
        "steps": [
            {"op": "goto", "url": site.url("/login")},
            {"op": "fill", "selector": "#username", "value": "alice"},
            {"op": "fill", "selector": "#password", "value": {"secret": "portal-pw"}},
            {"op": "click", "selector": "#go"},
            {"op": "assert_text", "text": "Balance", "timeout_s": 2},
        ],
        "login_signal": SIGNAL,
    }


@pytest.fixture
async def portal(clock: FakeClock) -> AsyncIterator[LoginSite]:
    site = await LoginSite(now=clock.monotonic).start()
    try:
        yield site
    finally:
        await site.stop()


@pytest.fixture
def http_browser() -> HttpBrowser:
    return HttpBrowser()


@pytest.fixture
def secret_store() -> MemorySecrets:
    return MemorySecrets({"portal-pw": "s3cret-pw"})


@pytest.fixture(autouse=True)
def _use_http_browser(engine: Engine, http_browser: HttpBrowser) -> None:
    engine.fetchers.replace("browser", BrowserFetcher(http_browser, engine.secrets))  # type: ignore[arg-type]


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def macro(client: httpx.AsyncClient, site: LoginSite, **kw: Any) -> int:
    r = await client.post("/macros", json={**macro_body(site), **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out


async def get(client: httpx.AsyncClient, bid: int) -> dict[str, Any]:
    out: dict[str, Any] = (await client.get(f"/bookmarks/{bid}")).json()
    return out


async def login_of(client: httpx.AsyncClient, bid: int) -> dict[str, Any]:
    out: dict[str, Any] = (await client.get(f"/bookmarks/{bid}/login")).json()
    return out


async def allow_stored_passwords(client: httpx.AsyncClient) -> None:
    assert (await client.put("/settings", json={"stored_password_logins": True})).status_code == 200


OPT_IN = {"login": {"allow_stored_password": True}}


def problems_of(sub: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if ev.type == "problem":
            out.append(ev.data)
    return out


# -- acceptance: a macro logs into the fixture site and detects a change behind it ---------


async def test_a_macro_logs_into_the_fixture_site_and_detects_a_change_behind_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await allow_stored_passwords(client)
    mid = await macro(client, portal)
    bid = await add(client, portal.url("/account"), macro_id=mid, fetch=OPT_IN)
    await settle(engine, clock)

    b = await get(client, bid)
    assert b["status"] == "ok" and b["latest_version_id"] and portal.logins == ["alice"]
    first = (await runs(client, bid))[-1]
    assert first["outcome"] == "first" and first["method"] == "browser"  # `auto` went to a browser
    assert b["actions"]["alert_privacy"] == "private"  # signed in: private by default (spec)

    portal.account_text = "Balance: 250. A new statement is ready."
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1
    changes = (await client.get(f"/bookmarks/{bid}/changes")).json()
    assert len(changes["items"]) == 1
    text = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    assert "new statement" in text
    # the session was reused: two checks, one login
    posts = [h for h in portal.hits_for("/login") if h.method == "POST"]
    assert portal.logins == ["alice"] and len(posts) == 1
    assert (await login_of(client, bid))["last_auto_login_at"] is not None


# -- the stored-password rules ------------------------------------------------------------


@pytest.mark.parametrize(
    "global_switch,bookmark_opt_in,logs_in",
    [(False, False, False), (True, False, False), (False, True, False), (True, True, True)],
)
async def test_a_stored_password_login_needs_both_the_global_switch_and_the_bookmarks_opt_in(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    global_switch: bool, bookmark_opt_in: bool, logs_in: bool,
) -> None:  # fmt: skip
    if global_switch:
        await allow_stored_passwords(client)
    mid = await macro(client, portal)
    fetch = OPT_IN if bookmark_opt_in else {}
    bid = await add(client, portal.url("/account"), macro_id=mid, fetch=fetch)
    await settle(engine, clock)
    b = await get(client, bid)
    if logs_in:
        assert b["status"] == "ok" and portal.logins == ["alice"]
    else:
        assert b["status"] == "needs_login" and portal.logins == []
        st = await login_of(client, bid)
        assert st["reason"].startswith("stored_password_off") and not st["stored_password_allowed"]
        assert st["stored_password"] is True


async def test_a_macro_without_a_password_is_not_a_stored_password_login(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    """The gate is about passwords: a macro that only clicks through needs no opt-in."""
    portal.users = {"": "x"}  # irrelevant; the macro below never logs in
    body = {
        "name": "consent",
        "steps": [{"op": "goto", "url": portal.url("/login")}, {"op": "click", "selector": "#go"}],
        "login_signal": SIGNAL,
    }
    mid = (await client.post("/macros", json=body)).json()["id"]
    assert (await client.get(f"/macros/{mid}")).json()["stored_password"] is False
    bid = await add(client, portal.url("/account"), macro_id=mid)
    await settle(engine, clock)
    # the macro ran (it was allowed) but did not get past the login: macro_failed, not stored_password_off
    assert (await login_of(client, bid))["reason"].startswith("macro_failed")


async def test_at_most_one_automated_login_per_six_hours(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await allow_stored_passwords(client)
    bid = await add(
        client, portal.url("/account"), macro_id=await macro(client, portal), fetch=OPT_IN
    )
    await settle(engine, clock)
    assert portal.logins == ["alice"] and (await get(client, bid))["status"] == "ok"
    sub = engine.events.subscribe()

    portal.expire_all()  # an hour in, the portal ends the session
    await advance(engine, clock, 3600, step=60)
    b = await get(client, bid)
    assert b["status"] == "needs_login" and portal.logins == ["alice"]  # it did NOT log in again
    assert (await login_of(client, bid))["reason"].startswith("login_rate_limited")
    kinds = [p["kind"] for p in problems_of(sub)]
    assert kinds == ["needs_login"] and len(toasts.shown) == 1  # exactly one alert

    await advance(engine, clock, 6 * 3600, step=1800)  # the bookmark stays paused
    n_runs = len(await runs(client, bid))
    await advance(engine, clock, 3600, step=600)
    assert len(await runs(client, bid)) == n_runs and portal.logins == ["alice"]

    r = await client.post(f"/bookmarks/{bid}/login/resume")  # now more than six hours on
    assert r.status_code == 200
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "ok" and portal.logins == ["alice", "alice"]


async def test_no_automated_login_after_a_failure_until_the_owner_resumes(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    secret_store: MemorySecrets, toasts: LogToastBackend,
) -> None:  # fmt: skip
    await allow_stored_passwords(client)
    secret_store.set("portal-pw", "the-wrong-password")
    bid = await add(
        client, portal.url("/account"), macro_id=await macro(client, portal), fetch=OPT_IN
    )
    await settle(engine, clock)
    st = await login_of(client, bid)
    assert st["needs_login"] and st["auto_login_blocked"] and portal.failed_logins == 1
    assert st["reason"].startswith("macro_failed") and "assert_text" in st["reason"]
    assert "the-wrong-password" not in st["reason"]
    assert len(toasts.shown) == 1

    secret_store.set("portal-pw", "s3cret-pw")  # the owner fixes the password...
    assert (
        await client.post(f"/bookmarks/{bid}/check", params={"force": "true"})
    ).status_code == 200
    await settle(engine, clock)
    st = await login_of(client, bid)  # ...but a failed login blocks further attempts by itself
    assert st["needs_login"] and portal.failed_logins == 1 and portal.logins == []
    assert st["reason"].startswith("login_blocked") and len(toasts.shown) == 1  # and no new alert

    # Resuming lifts the failure block, but the failed attempt still counts as this window's one
    # automated login: the six-hour limit is the portal-protection rule and is not bypassed.
    assert (await client.post(f"/bookmarks/{bid}/login/resume")).status_code == 200
    await settle(engine, clock)
    st = await login_of(client, bid)
    assert st["needs_login"] and not st["auto_login_blocked"] and portal.logins == []
    assert st["reason"].startswith("login_rate_limited") and st["next_auto_login_at"]
    assert portal.failed_logins == 1 and len(toasts.shown) == 2  # one more alert: a new expiry

    await advance(engine, clock, 6 * 3600 + 60, step=1800)
    assert (await client.post(f"/bookmarks/{bid}/login/resume")).status_code == 200
    await settle(engine, clock)
    st = await login_of(client, bid)
    assert not st["needs_login"] and not st["auto_login_blocked"] and portal.logins == ["alice"]


# -- acceptance: semi-attended sign-in ----------------------------------------------------


class FakeWindow:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def wait_closed(self) -> None:
        await self.closed.wait()

    async def close(self) -> None:
        self.closed.set()


class FakeOpener:
    def __init__(self) -> None:
        self.opened: list[tuple[Path, str]] = []
        self.window = FakeWindow()

    async def open(self, profile_dir: Path, url: str) -> FakeWindow:
        self.opened.append((profile_dir, url))
        self.window = FakeWindow()
        return self.window


async def sign_in_by_hand(site: LoginSite, browser: HttpBrowser, profile: Path) -> None:
    """What the owner does in the window: type the user name and password (and any code)."""
    jar = browser.profile_cookies(profile)
    async with httpx.AsyncClient(cookies=jar, follow_redirects=True) as c:
        await c.post(site.url("/login"), data={"username": "alice", "password": "s3cret-pw"})
        for cookie in c.cookies.jar:
            jar.jar.set_cookie(cookie)


SEMI = {"login": {"semi_attended": True, "signal": SIGNAL}}


async def test_semi_attended_sign_in_checks_immediately_and_an_expired_session_alerts_exactly_once(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser, toasts: LogToastBackend,
) -> None:  # fmt: skip
    opener = FakeOpener()
    engine.logins.signin.set_opener(opener)
    sub = engine.events.subscribe()
    bid = await add(client, portal.url("/account"), fetch=SEMI)
    await settle(engine, clock)
    # never signed in: the first check lands on the login page and says so, once
    assert (await get(client, bid))["status"] == "needs_login"
    assert len(toasts.shown) == 1 and "sign in" in toasts.shown[0].title.lower()
    assert [p["kind"] for p in problems_of(sub)] == ["needs_login"]
    assert portal.logins == []  # the engine never logs in by itself here

    started = (await client.post(f"/bookmarks/{bid}/signin")).json()
    assert started["state"] == "started" and opener.opened[0][1] == portal.url("/account")
    assert (await login_of(client, bid))["signing_in"] is True
    assert (
        await client.post(f"/bookmarks/{bid}/signin")
    ).status_code == 409  # one window at a time
    await client.post(f"/bookmarks/{bid}/check")  # a check would fight the owner for the profile
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["reason"] == "signing_in"

    await sign_in_by_hand(portal, http_browser, opener.opened[0][0])
    n_before = len(await runs(client, bid))
    done = (await client.post(f"/bookmarks/{bid}/signin/finish")).json()
    assert done["state"] == "finished"
    await settle(engine, clock)  # no time has passed: the check is immediate
    latest = (await runs(client, bid))[0]
    assert len(await runs(client, bid)) == n_before + 1
    assert latest["trigger"] == "manual" and latest["outcome"] == "first"
    b = await get(client, bid)
    assert (
        b["status"] == "ok"
        and b["latest_version_id"]
        and not (await login_of(client, bid))["signing_in"]
    )
    assert (await login_of(client, bid))["last_signin_at"] is not None

    # the session expires: exactly one needs-login alert, and the bookmark is paused
    portal.expire_all()
    shown_before = len(toasts.shown)
    await advance(engine, clock, 70, step=10)
    paused = await get(client, bid)
    assert paused["status"] == "needs_login"
    assert paused["next_due_at"] is None  # the list must not promise a check that will not happen
    assert len(toasts.shown) == shown_before + 1
    assert [p["kind"] for p in problems_of(sub)] == ["needs_login"]
    n_runs = len(await runs(client, bid))
    await advance(engine, clock, 3 * 3600, step=600)
    assert len(await runs(client, bid)) == n_runs and len(toasts.shown) == shown_before + 1
    # a manual check that finds it still signed out stays quiet
    await client.post(f"/bookmarks/{bid}/check")
    await settle(engine, clock)
    assert len(await runs(client, bid)) == n_runs + 1 and len(toasts.shown) == shown_before + 1

    # signing in again resumes it
    await client.post(f"/bookmarks/{bid}/signin")
    await sign_in_by_hand(portal, http_browser, opener.opened[-1][0])
    await client.post(f"/bookmarks/{bid}/signin/finish")
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "ok"
    await advance(engine, clock, 130, step=10)  # and it is scheduled again
    assert len(await runs(client, bid)) > n_runs + 2


async def test_closing_the_window_by_hand_counts_as_done(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    http_browser: HttpBrowser,
) -> None:  # fmt: skip
    opener = FakeOpener()
    engine.logins.signin.set_opener(opener)
    bid = await add(client, portal.url("/account"), fetch=SEMI)
    await settle(engine, clock)
    await client.post(f"/bookmarks/{bid}/signin")
    await sign_in_by_hand(portal, http_browser, opener.opened[0][0])
    opener.window.closed.set()  # the owner closes the window themselves
    for _ in range(50):
        await settle(engine, clock)
        if (await get(client, bid))["status"] == "ok":
            break
        await asyncio.sleep(0.02)
    assert (await get(client, bid))["status"] == "ok" and not engine.logins.signin.is_open(bid)


async def test_signing_in_needs_a_semi_attended_bookmark(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    engine.logins.signin.set_opener(FakeOpener())
    bid = await add(client, portal.url("/account"), enabled=False)
    assert (await client.post(f"/bookmarks/{bid}/signin")).status_code == 422
    assert (await client.post("/bookmarks/9999/signin")).status_code == 404
    done = (await client.post(f"/bookmarks/{bid}/signin/finish")).json()
    assert done["state"] == "idle"


async def test_needs_login_survives_a_restart_and_nothing_is_checked(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    data_dir: Any, settings_overrides: dict[str, Any], secret_store: MemorySecrets,
) -> None:  # fmt: skip
    bid = await add(client, portal.url("/account"), fetch=SEMI)
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "needs_login"
    hits = len(portal.hits_for("/account"))
    await engine.stop()

    second = Engine(
        data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
        secret_store=secret_store,
    )  # fmt: skip
    second.fetchers.replace("browser", BrowserFetcher(HttpBrowser(), second.secrets))  # type: ignore[arg-type]
    await second.start()
    try:
        await advance(second, clock, 600, step=60)
        assert len(portal.hits_for("/account")) == hits  # paused: not even a startup check
        row = await second.db.read(lambda c: c.execute("SELECT status FROM bookmark").fetchone())
        assert row["status"] == "needs_login"
    finally:
        await second.stop()
    # (the module-level `engine` fixture stops the first engine again; that is harmless)


# -- server auth and imported cookies ----------------------------------------------------


async def test_basic_auth_uses_the_stored_password_and_a_rejected_one_pauses_the_bookmark(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    secret_store: MemorySecrets, toasts: LogToastBackend,
) -> None:  # fmt: skip
    auth = {"auth": {"kind": "basic", "username": "alice", "secret_key": "portal-pw"}}
    bid = await add(client, portal.url("/basic"), fetch=auth, check_method="static")
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "ok"
    portal.account_text = "Balance: 999"
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1

    secret_store.set("portal-pw", "changed-on-the-portal")  # the password is changed elsewhere
    await advance(engine, clock, 70, step=10)
    st = await login_of(client, bid)
    assert (
        st["needs_login"]
        and st["reason"] == "auth_rejected"
        and st["signed_in_mode"] == "server_auth"
    )
    hits = len(portal.hits_for("/basic"))
    await advance(engine, clock, 3600, step=300)  # retrying would risk a lock-out: it does not
    assert len(portal.hits_for("/basic")) == hits
    assert len(toasts.shown) == 2  # the change, and one needs-login alert


async def test_cookie_import_checks_a_page_behind_the_cookie(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    data_dir: Any, toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await add(client, portal.url("/cookie-page"), check_method="static")
    await settle(engine, clock)
    first = await get(client, bid)  # 401 with no cookie, no login configured: a plain error
    assert first["consecutive_errors"] == 1 and (await runs(client, bid))[0]["reason"] == "http_401"
    assert first["status"] != "needs_login"

    good = "127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\tabc123\n"
    r = await client.post(f"/bookmarks/{bid}/cookies", json={"text": good})
    assert r.status_code == 200 and r.json()["count"] == 1 and "abc123" not in r.text
    path = engine.logins.cookie_path(bid)
    assert path.exists() and (await get(client, bid))["fetch"]["cookies_file"] == str(path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # a secret: only the owner reads it
    await client.post(f"/bookmarks/{bid}/check", params={"force": "true"})
    await settle(engine, clock)
    b = await get(client, bid)
    assert b["status"] == "ok" and b["actions"]["alert_privacy"] == "private"

    stale = "127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\told\n"  # the session behind it ends
    await client.post(f"/bookmarks/{bid}/cookies", json={"text": stale})
    await advance(engine, clock, 70, step=10)
    st = await login_of(client, bid)
    assert (
        st["needs_login"] and st["reason"] == "auth_rejected" and st["signed_in_mode"] == "cookies"
    )
    assert [t for t in toasts.shown if "sign in" in t.title.lower()].__len__() == 1

    r = await client.post(f"/bookmarks/{bid}/cookies", json={"text": good})  # a fresh export
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "ok"  # importing resumed it and it checked at once

    assert (await client.delete(f"/bookmarks/{bid}/cookies")).status_code == 204
    assert not path.exists() and (await get(client, bid))["fetch"]["cookies_file"] is None


async def test_cookie_import_rejects_files_that_are_not_cookie_files(
    client: httpx.AsyncClient, portal: LoginSite, tmp_path: Path
) -> None:
    bid = await add(client, portal.url("/cookie-page"), check_method="static", enabled=False)
    for body in (
        {"text": "this is not a cookie file"},
        {"text": ""},
        {"path": str(tmp_path / "nope")},
    ):
        r = await client.post(f"/bookmarks/{bid}/cookies", json=body)
        assert r.status_code == 422, body
    assert (await client.post(f"/bookmarks/{bid}/cookies", json={})).status_code == 422
    assert (await client.post("/bookmarks/9999/cookies", json={"text": "x"})).status_code == 404
    f = tmp_path / "c.txt"
    f.write_text("127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\tabc123\n")
    ok = await client.post(f"/bookmarks/{bid}/cookies", json={"path": str(f)})
    assert ok.status_code == 200 and ok.json()["count"] == 1
    status = (await client.get(f"/bookmarks/{bid}/cookies")).json()
    assert status["present"] and status["domains"] == ["127.0.0.1"]


# -- ACCESS HRA: watch the list, never open a notice ------------------------------------------


async def test_the_e_notices_list_is_watched_and_no_notice_pdf_is_ever_requested(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await allow_stored_passwords(client)
    bid = await add(
        client, portal.url("/enotices"), macro_id=await macro(client, portal), fetch=OPT_IN,
    )  # fmt: skip
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "ok"
    portal.notices.append("Notice 3: a requested document")
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # the new notice on the list is a change
    text = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    assert "Notice 3" in text
    assert portal.pdf_hits() == []  # opening a notice would clear its unread marker
    assert portal.hits_for("/enotices")  # the list page itself was read


# -- validation and privacy -------------------------------------------------------------------


async def test_sign_in_configuration_is_checked_when_it_is_saved(
    client: httpx.AsyncClient, portal: LoginSite
) -> None:
    mid = await macro(client, portal)
    nosignal = (
        await client.post("/macros", json={**macro_body(portal), "login_signal": None})
    ).json()["id"]
    url = portal.url("/account")

    async def create(**kw: Any) -> httpx.Response:
        return await client.post("/bookmarks", json={"url": url, "enabled": False, **kw})

    assert (await create(macro_id=9999)).status_code == 422  # no such macro
    r = await create(macro_id=mid, check_method="static")
    assert r.status_code == 422 and "browser method" in r.text
    r = await create(macro_id=nosignal)
    assert r.status_code == 422 and "login signal" in r.text
    assert (await create(macro_id=nosignal, fetch={"login": {"signal": SIGNAL}})).status_code == 201
    r = await create(fetch={"login": {"semi_attended": True}})
    assert r.status_code == 422 and "login signal" in r.text
    r = await client.post("/bookmarks", json={"url": "ftp://x/y", "macro_id": mid})
    assert r.status_code == 422 and "web pages" in r.text
    assert (await create(fetch={"login": {"min_login_interval_s": 60}})).status_code == 422  # < 1 h
    assert (await create(fetch={"proxy": "http://user:pw@proxy:8080"})).status_code == 422
    assert (await create(fetch={"proxy": "ftp://proxy"})).status_code == 422
    assert (await create(fetch={"proxy": "socks5://proxy:1080"})).status_code == 201


async def test_signed_in_bookmarks_default_to_private_unless_the_owner_chose(
    client: httpx.AsyncClient, portal: LoginSite
) -> None:
    mid = await macro(client, portal)
    url = portal.url("/account")
    plain = await add(client, url, enabled=False)
    assert (await get(client, plain))["actions"]["alert_privacy"] == "content"
    # becoming signed in later (a macro, cookies, a hand sign-in) flips the default too
    r = await client.patch(f"/bookmarks/{plain}", json={"macro_id": mid})
    assert r.status_code == 200 and r.json()["actions"]["alert_privacy"] == "private"
    chose = await add(client, url, enabled=False, actions={"alert_privacy": "content"})
    r = await client.patch(f"/bookmarks/{chose}", json={"macro_id": mid})
    assert r.json()["actions"]["alert_privacy"] == "content"  # an explicit choice is kept
    folder = (await client.post("/folders", json={"name": "f", "defaults": {
        "actions": {"alert_privacy": "content"}}})).json()["id"]  # fmt: skip
    inherited = await add(client, url, enabled=False, folder_id=folder, macro_id=mid)
    assert (await get(client, inherited))["actions"]["alert_privacy"] == "content"
    semi = await add(client, url, enabled=False, fetch=SEMI)
    assert (await get(client, semi))["actions"]["alert_privacy"] == "private"


# -- macros, secrets, sessions ----------------------------------------------------------------


async def test_macro_crud_and_what_it_refuses(client: httpx.AsyncClient, portal: LoginSite) -> None:
    mid = await macro(client, portal)
    listed = (await client.get("/macros")).json()
    assert [m["name"] for m in listed] == ["portal"] and listed[0]["secret_keys_used"] == [
        "portal-pw"
    ]
    assert listed[0]["stored_password"] and listed[0]["bookmarks"] == 0
    bad = {**macro_body(portal), "steps": [{"op": "fill", "selector": "#password", "value": "x"}]}
    r = await client.post("/macros", json=bad)
    assert r.status_code == 422 and "password field" in r.text
    assert (await client.post("/macros", json={"name": "x", "steps": []})).status_code == 422
    renamed = {**macro_body(portal), "name": "renamed"}
    assert (await client.put(f"/macros/{mid}", json=renamed)).json()["name"] == "renamed"
    bid = await add(client, portal.url("/account"), macro_id=mid, enabled=False)
    assert (await client.get(f"/macros/{mid}")).json()["bookmarks"] == 1
    assert (await client.delete(f"/macros/{mid}")).status_code == 204
    assert (await get(client, bid))["macro_id"] is None  # the bookmark outlives its macro
    assert (await client.get(f"/macros/{mid}")).status_code == 404
    assert (await client.put("/macros/999", json=renamed)).status_code == 404
    assert (await client.delete("/macros/999")).status_code == 404


async def test_secrets_are_write_only(
    client: httpx.AsyncClient, secret_store: MemorySecrets
) -> None:
    r = await client.put("/secrets/smtp.pw", json={"value": "hunter2"})
    assert r.status_code == 200 and r.json() == {
        "name": "smtp.pw",
        "exists": True,
        "available": True,
    }
    assert secret_store.get("smtp.pw") == "hunter2" and "hunter2" not in r.text
    got = await client.get("/secrets/smtp.pw")
    assert got.json()["exists"] is True and "hunter2" not in got.text
    gone = await client.delete("/secrets/smtp.pw")
    assert gone.json()["exists"] is False and secret_store.get("smtp.pw") is None
    assert (await client.get("/secrets/never-set")).json()["exists"] is False
    assert (await client.put("/secrets/bad name!", json={"value": "x"})).status_code == 422
    assert (await client.put("/secrets/ok", json={"value": ""})).status_code == 422


async def test_wiping_a_session_removes_the_profile_but_not_while_a_window_is_open(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    opener = FakeOpener()
    engine.logins.signin.set_opener(opener)
    bid = await add(client, portal.url("/account"), fetch=SEMI, enabled=False)
    profile = engine.logins.profile_dir(bid)
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("session")
    assert (await login_of(client, bid))["profile_exists"] is True
    await client.post(f"/bookmarks/{bid}/signin")
    assert (await client.delete(f"/bookmarks/{bid}/session")).status_code == 409
    await client.post(f"/bookmarks/{bid}/signin/finish")
    await settle(engine, clock)
    assert (await client.delete(f"/bookmarks/{bid}/session")).status_code == 204
    assert not profile.exists() and (await login_of(client, bid))["last_signin_at"] is None
    assert (await client.delete(f"/bookmarks/{bid}/session")).status_code == 204  # nothing: fine


async def test_deleting_a_bookmark_removes_its_saved_sessions_too(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    """A profile holds live session cookies and a cookie file holds a login: secrets that must not
    outlive the bookmark, however it is deleted."""
    opener = FakeOpener()
    engine.logins.signin.set_opener(opener)
    semi = await add(client, portal.url("/account"), fetch=SEMI, enabled=False)
    jar = await add(client, portal.url("/cookie-page"), check_method="static", enabled=False)
    for bid in (semi, jar):
        profile = engine.logins.profile_dir(bid)
        profile.mkdir(parents=True)
        (profile / "pagewatch-session.json").write_text('[{"name":"sid"}]')
    await client.post(
        f"/bookmarks/{jar}/cookies", json={"text": "127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\tabc123\n"}
    )
    cookie_file = engine.logins.cookie_path(jar)
    assert cookie_file.exists()
    await client.post(f"/bookmarks/{semi}/signin")  # a window is open when it is deleted
    window = opener.window
    assert engine.logins.signin.is_open(semi)

    assert (await client.delete(f"/bookmarks/{semi}")).status_code in (200, 204)
    assert not engine.logins.profile_dir(semi).exists() and window.closed.is_set()
    assert not engine.logins.signin.is_open(semi)
    r = await client.post(
        "/bookmarks/bulk", json={"ids": [jar], "action": "delete"}
    )  # the other route
    assert r.status_code == 200
    assert not engine.logins.profile_dir(jar).exists() and not cookie_file.exists()
    gone = await engine.db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM login_state").fetchone()[0]
    )
    assert gone == 0  # and the sign-in memory went with them (ON DELETE CASCADE)
