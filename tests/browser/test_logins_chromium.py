"""Logins against a real Chromium (marker ``browser``; run with ``pytest -m browser``): the
persistent profile, macro replay, session reuse, expiry, imported cookies, Basic auth and a
per-bookmark proxy, all through the real ``BrowserFetcher`` on the fixture portal."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.fetch.browser import BrowserFetcher, BrowserManager, PlaywrightLauncher
from pagewatch.engine.fetch.login import LoginPlan, MacroPermit
from pagewatch.engine.secrets import MemorySecrets
from pagewatch.models import LoginSignal, MacroDef, Settings
from tests.support.fetching import make_request
from tests.support.fixture_site import FixtureSite, article
from tests.support.login_site import LoginSite
from tests.support.proxy_servers import HttpProxy

CHROMIUM = os.environ.get("PAGEWATCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="no Chromium binary available"),
]

SETTINGS = Settings(browser_executable=CHROMIUM, browser_args=["--no-sandbox", "--disable-gpu"])
SIGNAL = LoginSignal(selector="input[type=password]")


def login_macro(site: LoginSite) -> MacroDef:
    return MacroDef.model_validate(
        {
            "name": "portal",
            "steps": [
                {"op": "goto", "url": site.url("/login")},
                {"op": "fill", "selector": "#username", "value": "alice"},
                {"op": "fill", "selector": "#password", "value": {"secret": "portal-pw"}},
                {"op": "click", "selector": "#go"},
                {"op": "assert_text", "text": "Balance"},
            ],
        }
    )


class Recorder:
    """Stands in for the engine's guard: lets the macro run and remembers what happened."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.permits = 0
        self.finished: list[bool] = []

    async def permit(self) -> MacroPermit:
        self.permits += 1
        return MacroPermit(self.allowed, None if self.allowed else "login_blocked")

    async def done(self, ok: bool) -> None:
        self.finished.append(ok)


@pytest.fixture
async def portal() -> AsyncIterator[LoginSite]:
    site = await LoginSite().start()
    try:
        yield site
    finally:
        await site.stop()


@pytest.fixture
async def manager() -> AsyncIterator[BrowserManager]:
    m = BrowserManager(lambda: SETTINGS, FakeClock(), PlaywrightLauncher())
    try:
        yield m
    finally:
        await m.close()


def plan(
    tmp_path: Path,
    site: LoginSite,
    rec: Recorder,
    *,
    macro: bool = True,
    password: str = "s3cret-pw",
) -> LoginPlan:
    secrets = MemorySecrets({"portal-pw": password})

    async def resolve(key: str) -> str | None:
        return secrets.get(key)

    return LoginPlan(
        profile_dir=tmp_path / "profiles" / "1",
        signal=SIGNAL,
        macro=login_macro(site) if macro else None,
        permit_macro=rec.permit,
        macro_finished=rec.done,
        resolve_secret=resolve,
    )


async def test_a_macro_logs_in_and_the_check_sees_the_page_behind_the_login(
    portal: LoginSite, manager: BrowserManager, tmp_path: Path
) -> None:
    rec = Recorder()
    fetcher = BrowserFetcher(manager)
    req = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
    req.login = plan(tmp_path, portal, rec)
    res = await fetcher.fetch(req)
    assert res.error is None and not res.login_required and res.macro_ran
    assert b"Balance: 100" in res.body and b"Please sign in" not in res.body
    assert rec.permits == 1 and rec.finished == [True] and portal.logins == ["alice"]


async def test_the_session_is_reused_so_later_checks_do_not_log_in_again(
    portal: LoginSite, manager: BrowserManager, tmp_path: Path
) -> None:
    rec = Recorder()
    fetcher = BrowserFetcher(manager)
    bodies = []
    for _ in range(3):
        req = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
        req.login = plan(tmp_path, portal, rec)
        res = await fetcher.fetch(req)
        assert res.error is None and not res.login_required
        bodies.append(res.macro_ran)
        portal.account_text = "Balance: 250"
    assert bodies == [True, False, False]  # the macro ran once; the profile kept the cookie
    assert portal.logins == ["alice"] and rec.permits == 1


async def test_an_expired_session_runs_the_macro_again_only_when_permitted(
    portal: LoginSite, manager: BrowserManager, tmp_path: Path
) -> None:
    fetcher = BrowserFetcher(manager)
    rec = Recorder()
    req = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
    req.login = plan(tmp_path, portal, rec)
    assert (await fetcher.fetch(req)).macro_ran
    portal.expire_all()
    again = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
    again.login = plan(tmp_path, portal, rec)
    res = await fetcher.fetch(again)
    assert res.macro_ran and res.error is None and len(portal.logins) == 2

    portal.expire_all()
    blocked = Recorder(allowed=False)
    third = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
    third.login = plan(tmp_path, portal, blocked)
    res = await fetcher.fetch(third)
    assert res.login_required and res.login_reason == "login_blocked" and res.body == b""
    assert len(portal.logins) == 2  # it did not log in


async def test_a_wrong_password_fails_the_macro_without_leaking_it(
    portal: LoginSite, manager: BrowserManager, tmp_path: Path
) -> None:
    rec = Recorder()
    req = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
    req.login = plan(tmp_path, portal, rec, password="the-wrong-password")
    res = await BrowserFetcher(manager).fetch(req)
    assert res.login_required and res.login_reason == "macro_failed"
    assert res.macro_error is not None and "the-wrong-password" not in res.macro_error
    assert "assert_text" in res.macro_error
    assert rec.finished == [False] and portal.failed_logins == 1


async def test_without_a_macro_a_signed_out_page_just_says_so(
    portal: LoginSite, manager: BrowserManager, tmp_path: Path
) -> None:
    req = make_request(portal.url("/account"), settings=SETTINGS, bookmark_id=1)
    req.login = plan(tmp_path, portal, Recorder(), macro=False)
    res = await BrowserFetcher(manager).fetch(req)
    assert res.login_required and res.login_reason == "login_signal" and res.error is None


async def test_imported_cookies_work_in_the_browser_too(
    portal: LoginSite, manager: BrowserManager, tmp_path: Path
) -> None:
    jar = tmp_path / "c.txt"
    jar.write_text(f"127.0.0.1\tFALSE\t/\tFALSE\t{int(time.time()) + 3600}\ttoken\tabc123\n")
    fetcher = BrowserFetcher(manager)
    page = portal.url("/cookie-page")
    plain = await fetcher.fetch(make_request(page, settings=SETTINGS))
    assert plain.error is not None and plain.error.status == 401
    ok = await fetcher.fetch(make_request(page, settings=SETTINGS, cookies_file=str(jar)))
    assert ok.error is None and b"Balance" in ok.body
    # the shared context stayed clean: the next ordinary fetch has no cookie
    after = await fetcher.fetch(make_request(page, settings=SETTINGS))
    assert after.error is not None and after.error.status == 401


async def test_basic_auth_in_the_browser_uses_the_stored_password(
    portal: LoginSite, manager: BrowserManager
) -> None:
    fetcher = BrowserFetcher(manager, MemorySecrets({"portal-pw": "s3cret-pw"}))
    auth = {"username": "alice", "secret_key": "portal-pw"}
    ok = await fetcher.fetch(make_request(portal.url("/basic"), settings=SETTINGS, auth=auth))
    assert ok.error is None and b"Balance" in ok.body
    missing = await BrowserFetcher(manager).fetch(
        make_request(portal.url("/basic"), settings=SETTINGS, auth=auth)
    )
    assert missing.error is not None and "portal-pw" in missing.error.message


async def test_a_bookmark_proxy_carries_browser_traffic(manager: BrowserManager) -> None:
    pages = await FixtureSite().start()
    pages.set("/news", article("Through the proxy"))
    proxy = await HttpProxy({"proxied.test": ("127.0.0.1", pages.port)}).start()
    try:
        res = await BrowserFetcher(manager).fetch(
            make_request("http://proxied.test/news", settings=SETTINGS, proxy=proxy.url)
        )
        assert res.error is None and b"Through the proxy" in res.body
        assert any(s.host == "proxied.test" for s in proxy.seen)
    finally:
        await proxy.stop()
        await pages.stop()
