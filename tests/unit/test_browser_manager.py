"""The shared browser's lifecycle (spec: Browser management), against a fake Playwright browser
and the fake clock. The page-level behaviour is covered with a real Chromium (marker ``browser``)."""

from __future__ import annotations

import asyncio
import builtins

import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.fetch import browser as br
from pagewatch.engine.fetch.browser import (
    BrowserManager,
    BrowserUnavailable,
    LaunchPlan,
    classify_browser_error,
)
from pagewatch.models import FetchErrorKind, Settings
from tests.support.fakes import FakeLauncher


def make(
    launcher: FakeLauncher | None = None, installer: object | None = None, **settings: object
) -> tuple[BrowserManager, FakeLauncher, FakeClock]:
    launcher = launcher or FakeLauncher()
    clock = FakeClock()
    s = Settings(**settings)  # type: ignore[arg-type]
    return BrowserManager(lambda: s, clock, launcher, installer), launcher, clock  # type: ignore[arg-type]


async def use(m: BrowserManager, **kw: object) -> None:
    async with m.page(**kw):  # type: ignore[arg-type]
        pass


async def test_the_browser_starts_lazily_and_only_once() -> None:
    m, launcher, _ = make()
    assert m.state == "stopped" and launcher.plans == []
    await use(m)
    await use(m)
    assert m.state == "running" and len(launcher.plans) == 1 and m.stats.pages_served == 2


async def test_edge_first_in_new_headless_mode_with_configured_arguments() -> None:
    m, launcher, _ = make(browser_args=["--no-sandbox"])
    await m.ensure()
    assert launcher.plans == [LaunchPlan("channel", "msedge", ("--no-sandbox", "--headless=new"))]


async def test_falls_back_to_the_bundled_chromium_when_edge_will_not_launch() -> None:
    m, launcher, _ = make(
        FakeLauncher(fail={"channel": "Chromium distribution 'msedge' is not found"})
    )
    await use(m)
    assert [p.source for p in launcher.plans] == ["channel", "bundled"]
    assert m.stats.fallbacks == 1 and m.state == "running"
    assert launcher.browsers[0].plan.source == "bundled"
    await use(m)
    assert len(launcher.plans) == 2  # the choice is kept: Edge is not retried on every page


async def test_an_explicit_executable_is_tried_first_and_falls_back_too() -> None:
    m, launcher, _ = make(
        FakeLauncher(fail={"executable": "bad path"}), browser_executable="/opt/chrome"
    )
    await use(m)
    assert [p.source for p in launcher.plans] == ["executable", "bundled"]
    assert launcher.plans[0].value == "/opt/chrome"


async def test_no_channel_means_bundled_only() -> None:
    m, launcher, _ = make(browser_channel=None)
    await m.ensure()
    assert [p.source for p in launcher.plans] == ["bundled"] and m.stats.fallbacks == 0


async def test_a_missing_bundled_chromium_is_downloaded_on_first_use() -> None:
    installs: list[int] = []
    launcher = FakeLauncher(
        fail={"channel": "no edge", "bundled": "BrowserType.launch: Executable doesn't exist at /x"}
    )

    async def installer() -> bool:
        installs.append(1)
        launcher.fail.pop("bundled")
        return True

    m, _, _ = make(launcher, installer)
    await use(m)
    assert installs == [1] and [p.source for p in launcher.plans] == [
        "channel",
        "bundled",
        "bundled",
    ]
    assert m.state == "running"


async def test_when_nothing_launches_the_browser_is_unavailable_with_a_cool_down() -> None:
    launcher = FakeLauncher(fail={"channel": "no edge", "bundled": "no chromium"})
    m, _, clock = make(launcher)
    with pytest.raises(BrowserUnavailable, match="no edge.*no chromium"):
        await use(m)
    assert m.state == "unavailable" and len(launcher.plans) == 2
    with pytest.raises(BrowserUnavailable):  # within the cool-down: no new attempt
        await use(m)
    assert len(launcher.plans) == 2
    launcher.fail.clear()
    clock.advance(br.UNAVAILABLE_RETRY_S + 1)
    await use(m)
    assert m.state == "running" and len(launcher.plans) == 3


async def test_a_failed_download_is_unavailable() -> None:
    async def installer() -> bool:
        return False

    m, _, _ = make(
        FakeLauncher(fail={"channel": "x", "bundled": "Executable doesn't exist"}), installer
    )
    with pytest.raises(BrowserUnavailable):
        await m.ensure()


async def test_at_most_three_pages_at_once() -> None:
    m, launcher, _ = make()
    gate = asyncio.Event()

    async def hold() -> None:
        async with m.page():
            await gate.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(7)]
    for _ in range(50):
        await asyncio.sleep(0)
    assert m.active_pages == 3 and launcher.browsers[0].live_pages == 3
    gate.set()
    await asyncio.gather(*tasks)
    assert launcher.browsers[0].max_live == 3 and m.stats.pages_served == 7


async def test_pages_share_one_context_and_tls_opt_out_gets_its_own() -> None:
    m, launcher, _ = make(default_user_agent="UA/1", global_proxy="http://proxy:8080")
    await use(m)
    await use(m)
    await use(m, verify_tls=False)
    contexts = launcher.browsers[0].contexts
    assert len(contexts) == 2
    secure, lax = contexts
    assert (
        secure.options["ignore_https_errors"] is False
        and lax.options["ignore_https_errors"] is True
    )
    assert secure.options["viewport"] == {"width": 1366, "height": 900}
    assert secure.options["device_scale_factor"] == 1 and secure.options["user_agent"] == "UA/1"
    assert secure.options["proxy"] == {"server": "http://proxy:8080"}
    assert len(secure.pages) == 2 and all(p.closed for p in secure.pages)


async def test_media_is_blocked_unless_asked_and_headers_are_set() -> None:
    m, launcher, _ = make()
    async with m.page(headers={"X-A": "1"}) as page:
        assert page.routed and page.headers == {"X-A": "1"}
    async with m.page(block_media=False) as page:
        assert not page.routed and page.headers is None


async def test_closes_after_ten_idle_minutes_and_restarts_on_demand() -> None:
    m, launcher, clock = make()
    await use(m)
    await clock.run_for(br.IDLE_CLOSE_S - 60)
    assert m.state == "running" and not launcher.browsers[0].closed
    await clock.run_for(120)
    assert m.state == "stopped" and launcher.browsers[0].closed and m.stats.idle_closes == 1
    assert launcher.browsers[0].contexts[0].closed
    await use(m)
    assert m.state == "running" and len(launcher.browsers) == 2


async def test_use_resets_the_idle_timer_and_a_busy_browser_is_never_closed() -> None:
    m, launcher, clock = make()
    await use(m)
    await clock.run_for(br.IDLE_CLOSE_S - 100)
    await use(m)  # the timer restarts here
    await clock.run_for(br.IDLE_CLOSE_S - 100)
    assert m.state == "running"
    async with m.page():
        await clock.run_for(br.IDLE_CLOSE_S * 3)  # a long page: nothing closes under it
        assert m.state == "running" and not launcher.browsers[0].closed
    await clock.run_for(br.IDLE_CLOSE_S + 60)
    assert m.state == "stopped" and launcher.browsers[0].closed


async def test_recycles_after_the_page_budget_and_lets_running_pages_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(br, "RECYCLE_AFTER_PAGES", 3)
    m, launcher, _ = make()
    await use(m)
    await use(m)
    async with m.page():  # the third page of the first browser, still running
        old = launcher.browsers[0]
        # the budget is reached when this page ends; meanwhile another page starts on the old one
        async with m.page():
            pass
        assert not old.closed
    # after the budget was hit the next page gets a fresh browser, the old one drains and closes
    await use(m)
    assert len(launcher.browsers) == 2 and old.closed and m.stats.recycles == 1
    assert launcher.browsers[1].live_pages == 0 and not launcher.browsers[1].closed


async def test_a_busy_retired_browser_closes_only_when_its_last_page_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(br, "RECYCLE_AFTER_PAGES", 2)
    m, launcher, _ = make()
    release = asyncio.Event()

    async def long_page() -> None:
        async with m.page():
            await release.wait()

    t = asyncio.create_task(long_page())
    for _ in range(20):
        await asyncio.sleep(0)
    await use(m)  # the second page: the budget is reached, the first is still running
    old = launcher.browsers[0]
    assert not old.closed and m.active_pages == 1
    await use(m)  # new pages go to a new browser
    assert len(launcher.browsers) == 2 and not old.closed
    release.set()
    await t
    assert old.closed


async def test_three_crashes_in_a_row_recycle_the_browser_and_success_resets_the_count() -> None:
    m, launcher, _ = make()
    await use(m)
    m.note_crash()
    m.note_crash()
    m.note_success()
    m.note_crash()
    m.note_crash()
    assert m.stats.recycles == 0
    m.note_crash()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert m.stats.recycles == 1 and launcher.browsers[0].closed
    await use(m)
    assert len(launcher.browsers) == 2


async def test_a_disconnected_browser_is_replaced() -> None:
    m, launcher, _ = make()
    await use(m)
    launcher.browsers[0].connected = False
    await use(m)
    assert len(launcher.browsers) == 2 and launcher.browsers[0].closed


async def test_close_shuts_everything_down_and_refuses_new_pages() -> None:
    m, launcher, clock = make()
    await use(m)
    await m.close()
    assert launcher.browsers[0].closed and launcher.closed and m.state == "stopped"
    with pytest.raises(BrowserUnavailable):
        await use(m)
    await clock.run_for(br.IDLE_CLOSE_S * 2)  # the cancelled idle task does not wake up


# -- error classification ---------------------------------------------------------------


class _PWError(Exception):
    pass


class TimeoutError(Exception):  # noqa: A001 - mimics playwright's TimeoutError by name
    pass


@pytest.mark.parametrize(
    ("message", "kind", "crashed"),
    [
        ("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x", FetchErrorKind.DNS, False),
        ("Page.goto: net::ERR_CERT_AUTHORITY_INVALID", FetchErrorKind.TLS, False),
        ("Page.goto: net::ERR_SSL_PROTOCOL_ERROR", FetchErrorKind.TLS, False),
        ("Page.goto: net::ERR_CONNECTION_REFUSED", FetchErrorKind.CONNECTION, False),
        ("Page.goto: net::ERR_INTERNET_DISCONNECTED", FetchErrorKind.CONNECTION, False),
        ("Page.goto: Download is starting", FetchErrorKind.BROWSER, False),
        (
            "Page.content: Target page, context or browser has been closed",
            FetchErrorKind.BROWSER,
            True,
        ),
        ("Browser has been closed", FetchErrorKind.BROWSER, True),
        ("Page crashed", FetchErrorKind.BROWSER, True),
        ("something unexpected", FetchErrorKind.BROWSER, False),
    ],
)
def test_classify_browser_errors(message: str, kind: FetchErrorKind, crashed: bool) -> None:
    err, died = classify_browser_error(_PWError(message))
    assert err.kind is kind and died is crashed


def test_playwright_timeouts_are_timeouts_not_crashes() -> None:
    err, died = classify_browser_error(TimeoutError("Timeout 45000ms exceeded."))
    assert err.kind is FetchErrorKind.TIMEOUT and err.transient and not died
    err, _ = classify_browser_error(builtins.TimeoutError())
    assert err.kind is FetchErrorKind.TIMEOUT
