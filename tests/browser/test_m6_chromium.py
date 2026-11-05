"""M6 acceptance criteria in a real Chromium, through the whole engine (marker ``browser``).

* A macro logs into the fixture login site and detects a change behind it.
* A semi-attended sign-in on the fixture site checks immediately; an expired session yields
  exactly one needs-login alert and pauses the bookmark.
* The ACCESS HRA E-Notices list is watched and no notice PDF is ever requested.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.fetch.browser import SESSION_COOKIES
from pagewatch.engine.logins.signin import PlaywrightSignInOpener
from tests.integration.test_m6_logins import OPT_IN, SEMI, add, get, login_of, macro, runs
from tests.support.login_site import LoginSite
from tests.support.sim import advance, settle

CHROMIUM = os.environ.get("PAGEWATCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="no Chromium binary available"),
]


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
        "worker_processes": 2, "toast_coalesce_s": 0.0,
        "browser_executable": CHROMIUM, "browser_args": ["--no-sandbox", "--disable-gpu"],
        "stored_password_logins": True,
    }  # fmt: skip


@pytest.fixture
async def portal() -> AsyncIterator[LoginSite]:
    site = await LoginSite().start()
    try:
        yield site
    finally:
        await site.stop()


async def until(predicate: Callable[[], Awaitable[bool]], limit_s: float = 20.0) -> None:
    for _ in range(int(limit_s / 0.1)):
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached in time")


# The shared helpers (`macro_body`, secrets, the fake clock) come from the default-suite module;
# these tests only swap the browser double for the real thing, which is the engine's default.


@pytest.fixture
def secret_store() -> Any:
    from pagewatch.engine.secrets import MemorySecrets

    return MemorySecrets({"portal-pw": "s3cret-pw"})


async def test_a_macro_logs_into_the_fixture_site_and_detects_a_change_behind_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await add(
        client, portal.url("/account"), macro_id=await macro(client, portal), fetch=OPT_IN
    )
    await settle(engine, clock)
    b = await get(client, bid)
    assert b["status"] == "ok" and b["latest_version_id"] and portal.logins == ["alice"]
    assert (await runs(client, bid))[-1]["method"] == "browser"

    portal.account_text = "Balance: 250. A new statement is ready."
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1
    assert (
        "new statement" in (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    )
    assert portal.logins == ["alice"]  # the second check reused the profile's session
    assert (engine.logins.profile_dir(bid) / SESSION_COOKIES).exists()


async def test_a_semi_attended_sign_in_checks_immediately_and_an_expired_session_alerts_once(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    engine.logins.signin.set_opener(PlaywrightSignInOpener(engine.browser, headless=True))
    sub = engine.events.subscribe()
    bid = await add(client, portal.url("/account"), fetch=SEMI)
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "needs_login" and len(toasts.shown) == 1

    assert (await client.post(f"/bookmarks/{bid}/signin")).json()["state"] == "started"
    window: Any = engine.logins.signin.window(bid)
    page = window.page
    await page.fill("#username", "alice")  # the owner types, including any one-time code
    await page.fill("#password", "s3cret-pw")
    await page.click("#go")
    await page.wait_for_selector("text=Balance")
    runs_before = len(await runs(client, bid))
    assert (await client.post(f"/bookmarks/{bid}/signin/finish")).json()["state"] == "finished"
    await settle(engine, clock)
    latest = (await runs(client, bid))[0]
    assert len(await runs(client, bid)) == runs_before + 1 and latest["trigger"] == "manual"
    assert latest["outcome"] == "first" and (await get(client, bid))["status"] == "ok"
    assert portal.logins == ["alice"]  # the engine itself never logged in

    portal.expire_all()
    shown = len(toasts.shown)
    await advance(engine, clock, 70, step=10)
    assert (await get(client, bid))["status"] == "needs_login"
    assert len(toasts.shown) == shown + 1
    kinds = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if ev.type == "problem":
            kinds.append(ev.data["kind"])
    assert kinds.count("needs_login") == 2  # the first (never signed in) and this one: no repeats
    n = len(await runs(client, bid))
    await advance(engine, clock, 2 * 3600, step=600)
    assert len(await runs(client, bid)) == n and len(toasts.shown) == shown + 1


async def test_the_session_survives_the_owner_closing_the_sign_in_window(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite
) -> None:
    """Chromium forgets session cookies when it closes; the cookies saved beside the profile are
    what keeps a portal that logs in with one working."""
    engine.logins.signin.set_opener(PlaywrightSignInOpener(engine.browser, headless=True))
    bid = await add(client, portal.url("/account"), fetch=SEMI)
    await settle(engine, clock)
    await client.post(f"/bookmarks/{bid}/signin")
    window: Any = engine.logins.signin.window(bid)
    await window.page.fill("#username", "alice")
    await window.page.fill("#password", "s3cret-pw")
    await window.page.click("#go")
    await window.page.wait_for_selector("text=Balance")
    saved = engine.logins.profile_dir(bid) / SESSION_COOKIES

    async def cookie_saved() -> bool:
        return saved.exists() and "sid" in saved.read_text()

    await until(cookie_saved, 5)  # copied out while the window is open
    await window.ctx.close()  # the owner closes the window
    await until(lambda: _checked(client, bid), 20)
    assert (await get(client, bid))["status"] == "ok"
    data = json.loads(saved.read_text())
    assert any(c["name"] == "sid" for c in data)
    if os.name == "posix":
        assert (saved.stat().st_mode & 0o077) == 0  # a secret: only the owner reads it


async def _checked(client: httpx.AsyncClient, bid: int) -> bool:
    b = await get(client, bid)
    return bool(b["status"] == "ok" and b["latest_version_id"])


async def test_the_e_notices_list_is_watched_and_no_notice_pdf_is_ever_requested(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, portal: LoginSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await add(
        client, portal.url("/enotices"), macro_id=await macro(client, portal), fetch=OPT_IN
    )
    await settle(engine, clock)
    assert (await get(client, bid))["status"] == "ok"
    portal.notices.append("Notice 3: a requested document")
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1
    assert "Notice 3" in (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    assert portal.pdf_hits() == []  # opening a notice would clear its unread marker
    assert (await login_of(client, bid))["stored_password"] is True
