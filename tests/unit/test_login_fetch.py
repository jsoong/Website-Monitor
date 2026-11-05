"""Static fetching with a login: Basic and Digest, imported cookies, and proxies."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pagewatch.engine.fetch.static import StaticFetcher, is_local_host
from pagewatch.engine.secrets import MemorySecrets
from pagewatch.models import FetchErrorKind, Settings
from tests.support.fetching import make_request
from tests.support.fixture_site import FixtureSite, article
from tests.support.login_site import LoginSite
from tests.support.proxy_servers import HttpProxy, Socks5Proxy

AUTH = {"username": "alice", "secret_key": "portal-pw"}
SECRETS = {"portal-pw": "s3cret-pw"}


@pytest.fixture
async def portal() -> AsyncIterator[LoginSite]:
    site = await LoginSite().start()
    try:
        yield site
    finally:
        await site.stop()


@pytest.fixture
async def fixture_site() -> AsyncIterator[FixtureSite]:
    site = await FixtureSite().start()
    try:
        yield site
    finally:
        await site.stop()


def cookie_file(tmp_path: Path, token: str, name: str = "c.txt") -> Path:
    path = tmp_path / name
    path.write_text(f"127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\t{token}\n")
    return path


# -- server auth ------------------------------------------------------------------------


@pytest.mark.parametrize("kind,path", [("basic", "/basic"), ("digest", "/digest")])
async def test_basic_and_digest_logins_use_the_stored_password(
    portal: LoginSite, kind: str, path: str
) -> None:
    fetcher = StaticFetcher(MemorySecrets(SECRETS))
    res = await fetcher.fetch(make_request(portal.url(path), auth={**AUTH, "kind": kind}))
    assert res.error is None and b"Balance: 100" in res.body
    await fetcher.aclose()


@pytest.mark.parametrize("kind,path", [("basic", "/basic"), ("digest", "/digest")])
async def test_a_wrong_password_is_a_401_not_a_crash(
    portal: LoginSite, kind: str, path: str
) -> None:
    fetcher = StaticFetcher(MemorySecrets({"portal-pw": "nope"}))
    res = await fetcher.fetch(make_request(portal.url(path), auth={**AUTH, "kind": kind}))
    assert res.error is not None and res.error.status == 401
    await fetcher.aclose()


async def test_a_missing_secret_says_so_without_contacting_the_site(portal: LoginSite) -> None:
    fetcher = StaticFetcher(MemorySecrets())
    res = await fetcher.fetch(make_request(portal.url("/basic"), auth=AUTH))
    assert res.error is not None and res.error.kind is FetchErrorKind.PARSE
    assert "portal-pw" in res.error.message and "s3cret-pw" not in res.error.message
    assert portal.hits == []
    await fetcher.aclose()


async def test_a_basic_password_is_never_sent_over_plain_http_to_the_internet() -> None:
    fetcher = StaticFetcher(MemorySecrets(SECRETS))
    res = await fetcher.fetch(make_request("http://portal.example.com/", auth=AUTH))
    assert res.error is not None and "plain http" in res.error.message
    await fetcher.aclose()


def test_local_hosts_are_recognised() -> None:
    assert all(is_local_host(h) for h in ("127.0.0.1", "localhost", "192.168.1.5", "::1"))
    assert not any(is_local_host(h) for h in ("example.com", "8.8.8.8", None))


# -- imported cookies -------------------------------------------------------------------


async def test_imported_cookies_open_the_protected_page(portal: LoginSite, tmp_path: Path) -> None:
    fetcher = StaticFetcher()
    page = portal.url("/cookie-page")
    plain = await fetcher.fetch(make_request(page, bookmark_id=1))
    assert plain.error is not None and plain.error.status == 401
    ok = await fetcher.fetch(
        make_request(page, bookmark_id=1, cookies_file=str(cookie_file(tmp_path, "abc123")))
    )
    assert ok.error is None and b"Balance" in ok.body
    await fetcher.aclose()


async def test_each_bookmark_keeps_its_own_cookie_session(
    portal: LoginSite, tmp_path: Path
) -> None:
    fetcher = StaticFetcher()
    good = str(cookie_file(tmp_path, "abc123", "good.txt"))
    bad = str(cookie_file(tmp_path, "stale", "bad.txt"))
    page = portal.url("/cookie-page")
    a = await fetcher.fetch(make_request(page, bookmark_id=1, cookies_file=good))
    b = await fetcher.fetch(make_request(page, bookmark_id=2, cookies_file=bad))
    a_again = await fetcher.fetch(make_request(page, bookmark_id=1, cookies_file=good))
    assert a.error is None and a_again.error is None
    assert b.error is not None and b.error.status == 401  # not served bookmark 1's session
    await fetcher.aclose()


async def test_reimporting_the_cookie_file_takes_effect_on_the_next_check(
    portal: LoginSite, tmp_path: Path
) -> None:
    fetcher = StaticFetcher()
    path = cookie_file(tmp_path, "expired-token")
    page = portal.url("/cookie-page")
    first = await fetcher.fetch(make_request(page, bookmark_id=7, cookies_file=str(path)))
    assert first.error is not None and first.error.status == 401
    path.write_text(path.read_text().replace("expired-token", "abc123"))
    os.utime(path, (time.time() + 5, time.time() + 5))  # a coarse file clock must not hide it
    second = await fetcher.fetch(make_request(page, bookmark_id=7, cookies_file=str(path)))
    assert second.error is None
    await fetcher.aclose()


async def test_a_missing_cookie_file_asks_for_a_new_import(
    portal: LoginSite, tmp_path: Path
) -> None:
    fetcher = StaticFetcher()
    res = await fetcher.fetch(
        make_request(
            portal.url("/cookie-page"), bookmark_id=1, cookies_file=str(tmp_path / "gone.txt")
        )
    )
    assert res.error is not None and res.error.kind is FetchErrorKind.PARSE
    assert "import it again" in res.error.message and portal.hits == []
    await fetcher.aclose()


# -- proxies ----------------------------------------------------------------------------


@pytest.fixture
async def pages(fixture_site: FixtureSite) -> FixtureSite:
    fixture_site.set("/news", article("Via the proxy"))
    return fixture_site


async def test_a_bookmark_proxy_carries_the_request(pages: FixtureSite) -> None:
    proxy = await HttpProxy({"proxied.test": ("127.0.0.1", pages.port)}).start()
    try:
        fetcher = StaticFetcher()
        res = await fetcher.fetch(make_request("http://proxied.test/news", proxy=proxy.url))
        assert res.error is None and b"Via the proxy" in res.body
        assert [s.host for s in proxy.seen] == ["proxied.test"]
        direct = await fetcher.fetch(make_request("http://proxied.test/news"))
        assert direct.error is not None  # the name resolves nowhere without the proxy
        await fetcher.aclose()
    finally:
        await proxy.stop()


async def test_the_bookmark_proxy_wins_over_the_global_one(pages: FixtureSite) -> None:
    mine = await HttpProxy({"proxied.test": ("127.0.0.1", pages.port)}).start()
    other = await HttpProxy({}).start()
    try:
        fetcher = StaticFetcher()
        res = await fetcher.fetch(
            make_request(
                "http://proxied.test/news",
                proxy=mine.url,
                settings=Settings(global_proxy=other.url),
            )
        )
        assert res.error is None and other.seen == [] and len(mine.seen) == 1
        via_global = await fetcher.fetch(
            make_request("http://proxied.test/news", settings=Settings(global_proxy=mine.url))
        )
        assert via_global.error is None and len(mine.seen) == 2
        await fetcher.aclose()
    finally:
        await mine.stop()
        await other.stop()


async def test_a_proxy_login_comes_from_the_secret_store(pages: FixtureSite) -> None:
    proxy = await HttpProxy(
        {"proxied.test": ("127.0.0.1", pages.port)}, ("bob", "p@ss w/rd")
    ).start()
    try:
        fetcher = StaticFetcher(MemorySecrets({"proxy-pw": "p@ss w/rd"}))
        cfg = {"proxy": proxy.url, "proxy_username": "bob", "proxy_secret_key": "proxy-pw"}
        ok = await fetcher.fetch(make_request("http://proxied.test/news", **cfg))
        assert ok.error is None and proxy.seen[-1].proxy_authorization is not None
        wrong = StaticFetcher(MemorySecrets({"proxy-pw": "wrong"}))
        denied = await wrong.fetch(make_request("http://proxied.test/news", **cfg))
        assert denied.error is not None and denied.error.status == 407
        missing = await StaticFetcher().fetch(make_request("http://proxied.test/news", **cfg))
        assert missing.error is not None and missing.error.kind is FetchErrorKind.PARSE
        assert "proxy-pw" in missing.error.message
        await fetcher.aclose()
        await wrong.aclose()
    finally:
        await proxy.stop()


async def test_socks5_proxies_work_with_and_without_a_login(pages: FixtureSite) -> None:
    open_proxy = await Socks5Proxy({"socks.test": ("127.0.0.1", pages.port)}).start()
    locked = await Socks5Proxy(
        {"socks.test": ("127.0.0.1", pages.port)}, ("carol", "hunter2")
    ).start()
    try:
        fetcher = StaticFetcher(MemorySecrets({"socks-pw": "hunter2"}))
        ok = await fetcher.fetch(make_request("http://socks.test/news", proxy=open_proxy.url))
        assert ok.error is None and b"Via the proxy" in ok.body
        assert open_proxy.requested == ["socks.test"]
        cfg = {"proxy": locked.url, "proxy_username": "carol", "proxy_secret_key": "socks-pw"}
        authed = await fetcher.fetch(make_request("http://socks.test/news", **cfg))
        assert authed.error is None and locked.requested == ["socks.test"]
        anonymous = await fetcher.fetch(make_request("http://socks.test/news", proxy=locked.url))
        assert anonymous.error is not None  # the server wants a login the config did not give
        await fetcher.aclose()
    finally:
        await open_proxy.stop()
        await locked.stop()
