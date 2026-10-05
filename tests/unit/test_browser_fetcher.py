"""BrowserFetcher / ScreenshotFetcher against scripted pages: what is asked of the page, how the
result is shaped and how failures are classified. A real Chromium is used in tests/browser."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.config import Resolved
from pagewatch.engine.fetch import browser as br
from pagewatch.engine.fetch.base import FetchRequest
from pagewatch.engine.fetch.browser import BrowserFetcher, BrowserManager
from pagewatch.engine.fetch.screenshot import ScreenshotFetcher
from pagewatch.models import (
    ActionsConfig,
    FetchConfig,
    FetchErrorKind,
    FilterConfig,
    GateConfig,
    ScheduleConfig,
    Settings,
)
from tests.support.fakes import FakeContext, FakeLauncher, FakePage


class Resp:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._headers = headers or {"Content-Type": "text/html"}

    async def all_headers(self) -> dict[str, str]:
        return self._headers


class Mouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.wheels: list[tuple[float, float]] = []

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y))

    async def wheel(self, dx: float, dy: float) -> None:
        self.wheels.append((dx, dy))


class Keyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class ScriptedPage(FakePage):
    """The slice of Playwright's Page the fetcher uses."""

    url = "https://example.test/final"
    html = "<html><body><p>rendered</p></body></html>"
    response: Resp | None = Resp()
    goto_error: BaseException | None = None
    hang = False

    def __init__(self, context: FakeContext) -> None:
        super().__init__(context)
        self.mouse, self.keyboard = Mouse(), Keyboard()
        self.main_frame = object()
        self.frames: list[Any] = [self.main_frame]
        self.calls: list[str] = []
        self.shot_kwargs: dict[str, Any] = {}
        self.goto_args: dict[str, Any] = {}

    async def goto(self, url: str, **kw: Any) -> Resp | None:
        self.goto_args = {"url": url, **kw}
        if self.hang:
            await asyncio.sleep(3600)
        if self.goto_error is not None:
            raise self.goto_error
        return self.response

    async def content(self) -> str:
        return self.html

    async def evaluate(self, js: str, *a: Any) -> Any:
        self.calls.append(f"evaluate:{js[:20]}")

    async def add_style_tag(self, **kw: Any) -> None:
        self.calls.append("style")

    async def wait_for_load_state(self, *a: Any, **kw: Any) -> None:
        self.calls.append("networkidle")

    async def screenshot(self, **kw: Any) -> bytes:
        self.shot_kwargs = kw
        return b"\x89PNG\r\n\x1a\nfake"


class PW(Exception):
    pass


class PWTimeout(Exception):
    pass


PWTimeout.__name__ = "TimeoutError"


def setup(
    page_cls: type[ScriptedPage] = ScriptedPage, **settings: Any
) -> tuple[BrowserManager, list[ScriptedPage], FakeLauncher]:
    pages: list[ScriptedPage] = []

    def factory(ctx: FakeContext) -> ScriptedPage:
        p = page_cls(ctx)
        pages.append(p)
        return p

    launcher = FakeLauncher(page_factory=factory)  # type: ignore[arg-type]
    s = Settings(**settings)
    return BrowserManager(lambda: s, FakeClock(), launcher), pages, launcher


def request(url: str = "https://example.test/start", **fetch: Any) -> FetchRequest:
    return FetchRequest(
        url=url,
        resolved=Resolved(
            schedule=ScheduleConfig(), fetch=FetchConfig.model_validate(fetch), filter=FilterConfig(),
            gate=GateConfig(), actions=ActionsConfig(), overrides={},
        ),
        settings=Settings(),
    )  # fmt: skip


async def test_the_dom_comes_back_as_utf8_html_with_the_final_url() -> None:
    m, pages, _ = setup()
    page_html = "<html><body><p>Zürich — café</p></body></html>"
    pages_html = type("P", (ScriptedPage,), {"html": page_html})
    m, pages, _ = setup(pages_html)
    res = await BrowserFetcher(m).fetch(request())
    assert res.error is None and res.status == 200 and res.final_url == "https://example.test/final"
    assert res.body.decode("utf-8") == page_html and res.content_type.startswith("text/html")
    assert res.headers == {"content-type": "text/html"} and res.screenshot_png is None
    p = pages[0]
    assert p.goto_args["wait_until"] == "load" and p.goto_args["timeout"] == 45000
    assert p.routed and p.closed  # images, media and fonts blocked; the page is closed


async def test_user_agent_and_headers_go_to_the_page() -> None:
    m, pages, _ = setup()
    await BrowserFetcher(m).fetch(request(headers={"X-Key": "1"}, user_agent="Bot/2"))
    assert pages[0].headers == {"X-Key": "1", "User-Agent": "Bot/2"}


async def test_scroll_mouse_keys_and_delay_follow_the_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(br, "SCROLL_GAP_S", 0.0)
    m, pages, _ = setup()
    res = await BrowserFetcher(m).fetch(
        request(browser={"scroll_count": 3, "mouse_moves": 2, "keys": ["End", "Escape"],
                         "delay_after_load_s": 0.01})
    )  # fmt: skip
    assert res.error is None
    p = pages[0]
    assert p.mouse.wheels == [(0, 800)] * 3 and len(p.mouse.moves) == 2
    assert p.keyboard.pressed == ["End", "Escape"]


async def test_the_screenshot_fetcher_loads_media_and_captures_a_png() -> None:
    m, pages, _ = setup()
    res = await ScreenshotFetcher(m).fetch(request())
    p = pages[0]
    assert not p.routed  # images, media and fonts are what is being compared
    assert res.screenshot_png and res.screenshot_png.startswith(b"\x89PNG")
    assert p.shot_kwargs == {"type": "png", "animations": "disabled", "caret": "hide",
                             "scale": "css", "full_page": True}  # fmt: skip
    assert "style" in p.calls and "networkidle" in p.calls  # animations off, page settled


async def test_screenshot_clip_and_viewport_only_options() -> None:
    m, pages, _ = setup()
    clip = {"x": 10, "y": 20, "w": 300, "h": 200}
    await ScreenshotFetcher(m).fetch(request(browser={"clip": clip}))
    assert pages[0].shot_kwargs["clip"] == {"x": 10, "y": 20, "width": 300, "height": 200}
    assert pages[0].shot_kwargs["full_page"] is True  # a clip is in page coordinates
    await ScreenshotFetcher(m).fetch(request(browser={"full_page": False}))
    assert pages[1].shot_kwargs["full_page"] is False and "clip" not in pages[1].shot_kwargs


async def test_http_errors_carry_status_and_retry_after() -> None:
    class Busy(ScriptedPage):
        response = Resp(503, {"Retry-After": "120", "content-type": "text/html"})

    m, _, _ = setup(Busy)
    res = await BrowserFetcher(m).fetch(request())
    assert res.error is not None and res.error.kind is FetchErrorKind.HTTP
    assert res.error.status == 503 and res.error.retry_after_s == 120 and res.error.transient

    class Gone(ScriptedPage):
        response = Resp(404)

    m, _, _ = setup(Gone)
    res = await BrowserFetcher(m).fetch(request())
    assert res.error is not None and res.error.reason == "http_404" and not res.error.transient


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (PW("Page.goto: net::ERR_NAME_NOT_RESOLVED"), FetchErrorKind.DNS),
        (PW("Page.goto: net::ERR_CERT_DATE_INVALID"), FetchErrorKind.TLS),
        (PW("Page.goto: net::ERR_CONNECTION_REFUSED"), FetchErrorKind.CONNECTION),
        (PWTimeout("Timeout 45000ms exceeded."), FetchErrorKind.TIMEOUT),
        (PW("Page.goto: Download is starting"), FetchErrorKind.BROWSER),
    ],
)
async def test_navigation_failures_are_classified(
    error: BaseException, kind: FetchErrorKind
) -> None:
    class Broken(ScriptedPage):
        goto_error = error

    m, pages, _ = setup(Broken)
    res = await BrowserFetcher(m).fetch(request())
    assert res.error is not None and res.error.kind is kind and res.body == b""
    assert pages[0].closed  # the page is closed on failure too


async def test_three_crashed_fetches_recycle_the_browser() -> None:
    class Dying(ScriptedPage):
        goto_error = PW("Target page, context or browser has been closed")

    m, _, launcher = setup(Dying)
    f = BrowserFetcher(m)
    for _ in range(3):
        res = await f.fetch(request())
        assert res.error is not None and res.error.kind is FetchErrorKind.BROWSER
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert m.stats.recycles == 1 and launcher.browsers[0].closed


async def test_a_good_fetch_resets_the_crash_count() -> None:
    m, pages, launcher = setup()
    f = BrowserFetcher(m)
    m.note_crash()
    m.note_crash()
    assert (await f.fetch(request())).error is None
    m.note_crash()
    m.note_crash()
    assert m.stats.recycles == 0 and not launcher.browsers[0].closed


async def test_a_page_that_hangs_hits_the_hard_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Hung(ScriptedPage):
        hang = True

    monkeypatch.setattr(br, "PAGE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(br, "HARD_GRACE_S", 0.0)
    m, pages, _ = setup(Hung)
    res = await BrowserFetcher(m).fetch(request())
    assert res.error is not None and res.error.kind is FetchErrorKind.TIMEOUT
    assert pages[0].closed and m.active_pages == 0


async def test_no_browser_is_a_browser_error_naming_why() -> None:
    launcher = FakeLauncher(fail={"channel": "no edge", "bundled": "no chromium"})
    m = BrowserManager(lambda: Settings(), FakeClock(), launcher)
    res = await BrowserFetcher(m).fetch(request())
    assert res.error is not None and res.error.kind is FetchErrorKind.BROWSER
    assert "no browser could be started" in res.error.message and "no chromium" in res.error.message


async def test_an_oversized_dom_is_too_large() -> None:
    class Huge(ScriptedPage):
        html = "<p>" + "x" * 5000 + "</p>"

    m, _, _ = setup(Huge)
    res = await BrowserFetcher(m).fetch(request(max_bytes=2048))
    assert res.error is not None and res.error.kind is FetchErrorKind.TOO_LARGE


async def test_tls_opt_out_uses_the_lax_context() -> None:
    m, _, launcher = setup()
    await BrowserFetcher(m).fetch(request(verify_tls=False))
    assert launcher.browsers[0].contexts[0].options["ignore_https_errors"] is True


# -- iframes ----------------------------------------------------------------------------


class Element:
    def __init__(self) -> None:
        self.inlined: list[str] = []

    async def evaluate(self, js: str, html: str) -> None:
        assert "replaceWith" in js and "data-pw-iframe" in js
        self.inlined.append(html)


class Frame:
    def __init__(self, url: str, body: str, *, fail: bool = False) -> None:
        self.url = url
        self.body = body
        self.fail = fail
        self.element = Element()

    async def evaluate(self, js: str) -> str:
        if self.fail:
            raise PW("Frame was detached")
        return self.body

    async def frame_element(self) -> Element:
        return self.element


async def test_same_origin_iframes_are_inlined_cross_origin_are_not() -> None:
    class P:
        url = "https://example.test/page"
        main_frame = object()

    page = P()
    same = Frame("https://example.test/frame", "<p>inside</p>")
    other = Frame("https://ads.test/frame", "<p>ad</p>")
    blank = Frame("about:blank", "<p>srcdoc</p>")
    gone = Frame("https://example.test/gone", "x", fail=True)
    page.frames = [page.main_frame, same, other, blank, gone]  # type: ignore[attr-defined]
    n = await br.inline_iframes(page)
    assert n == 2
    assert same.element.inlined == ["<p>inside</p>"] and blank.element.inlined == ["<p>srcdoc</p>"]
    assert other.element.inlined == [] and gone.element.inlined == []


async def test_nested_frames_are_inlined_deepest_first() -> None:
    order: list[str] = []

    class Nested(Frame):
        async def frame_element(self) -> Element:
            order.append(self.url)
            return self.element

    class P:
        url = "https://example.test/page"
        main_frame = object()

    page = P()
    outer = Nested("https://example.test/outer", "<iframe></iframe>")
    inner = Nested("https://example.test/inner", "<p>deep</p>")
    page.frames = [page.main_frame, outer, inner]  # type: ignore[attr-defined]  # document order
    assert await br.inline_iframes(page) == 2
    assert order == ["https://example.test/inner", "https://example.test/outer"]
