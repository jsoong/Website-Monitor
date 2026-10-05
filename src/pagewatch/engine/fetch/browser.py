"""The browser fetcher and the one shared browser behind it (spec: Fetch layer, Browser management).

* One browser, launched on the first page that needs it: the system Microsoft Edge through
  Playwright's ``msedge`` channel in new headless mode; if Edge is missing or will not launch,
  Playwright's bundled Chromium (downloaded on first use). It allows at most 3 pages at once and
  is closed after 10 idle minutes (on the engine's injectable clock).
* All pages share one ephemeral context (a second one for bookmarks that opt out of TLS
  verification). Images, media and fonts are blocked except for the screenshot method.
* 45 s hard timeout per page. The browser is recycled after 500 pages or 3 consecutive crashes;
  a recycled browser finishes its running pages before it is closed.
* The fetcher waits for ``load`` plus ``delay_after_load_s``, scrolls ``scroll_count`` times
  (800 px, 500 ms apart), can move the mouse and press keys, folds same-origin iframes into the page
  and returns the serialised DOM.

Playwright is reached only through ``Launcher`` (default: ``PlaywrightLauncher``), so the lifecycle
logic is tested with a fake browser and the page logic with a real Chromium (marker ``browser``).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pagewatch.engine.clock import Clock
from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.fetch.static import parse_retry_after
from pagewatch.engine.logs import get_logger
from pagewatch.models import BrowserOptions, FetchErrorKind, Settings

log = get_logger("pagewatch.browser")

VIEWPORT = {"width": 1366, "height": 900}
MAX_PAGES = 3
IDLE_CLOSE_S = 600.0
IDLE_CHECK_S = 30.0
RECYCLE_AFTER_PAGES = 500
RECYCLE_AFTER_CRASHES = 3
PAGE_TIMEOUT_S = 45.0
HARD_GRACE_S = 5.0  # on top of the navigation timeout: bounds a page that hangs after load
SCROLL_PX = 800
SCROLL_GAP_S = 0.5
UNAVAILABLE_RETRY_S = 300.0
INSTALL_TIMEOUT_S = 900.0
BLOCKED_RESOURCES = frozenset({"image", "media", "font"})
NO_MOTION_CSS = (
    "*,*::before,*::after{animation:none!important;transition:none!important;"
    "caret-color:transparent!important;scroll-behavior:auto!important}"
)

BrowserState = Literal["stopped", "running", "unavailable"]


class BrowserUnavailable(RuntimeError):
    """No browser could be started (Edge and the bundled Chromium both failed)."""


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    source: Literal["executable", "channel", "bundled"]
    value: str | None
    args: tuple[str, ...] = ()


Launcher = Callable[[LaunchPlan], Awaitable[Any]]
Installer = Callable[[], Awaitable[bool]]


class PlaywrightLauncher:
    """Starts Playwright lazily and launches Chromium-family browsers through it."""

    def __init__(self) -> None:
        self._pw: Any = None

    async def __call__(self, plan: LaunchPlan) -> Any:
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        kwargs: dict[str, Any] = {"headless": True, "args": list(plan.args)}
        if plan.source == "executable":
            kwargs["executable_path"] = plan.value
        elif plan.source == "channel":
            kwargs["channel"] = plan.value
        return await self._pw.chromium.launch(**kwargs)

    async def aclose(self) -> None:
        pw, self._pw = self._pw, None
        if pw is not None:
            with suppress(Exception):
                await pw.stop()


async def install_bundled_chromium() -> bool:
    """``playwright install chromium``: the fallback browser is downloaded on first use."""
    log.info("browser_install_start")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        async with asyncio.timeout(INSTALL_TIMEOUT_S):
            code = await proc.wait()
    except Exception as exc:
        log.warning("browser_install_failed", error=str(exc)[:200])
        return False
    log.info("browser_install_done", exit_code=code)
    return code == 0


def _missing_executable(exc: BaseException) -> bool:
    text = str(exc)
    return "Executable doesn't exist" in text or "playwright install" in text


class _Generation:
    """One launched browser and its contexts. Retired when it is recycled; closed once idle."""

    __slots__ = ("active", "browser", "contexts", "lock", "pages", "plan", "retired")

    def __init__(self, browser: Any, plan: LaunchPlan) -> None:
        self.browser = browser
        self.plan = plan
        self.contexts: dict[bool, Any] = {}  # verify_tls -> context
        self.lock = asyncio.Lock()
        self.active = 0
        self.pages = 0
        self.retired = False


@dataclass(slots=True)
class BrowserStats:
    launches: int = 0
    pages_served: int = 0
    recycles: int = 0
    idle_closes: int = 0
    fallbacks: int = 0
    plans: list[str] = field(default_factory=list)


class BrowserManager:
    def __init__(
        self,
        settings: Callable[[], Settings],
        clock: Clock,
        launcher: Launcher | None = None,
        installer: Installer | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._launcher: Launcher = launcher or PlaywrightLauncher()
        self._installer: Installer = installer or install_bundled_chromium
        self._slots: asyncio.Semaphore | None = None
        self._lock = asyncio.Lock()
        self._current_gen: _Generation | None = None
        self._gens: set[_Generation] = set()
        self._idle_task: asyncio.Task[None] | None = None
        self._last_used = clock.monotonic()
        self._unavailable_until = 0.0
        self._unavailable_reason = ""
        self._crashes = 0
        self._state: BrowserState = "stopped"
        self._closed = False
        self._tasks: set[asyncio.Task[None]] = set()
        self.stats = BrowserStats()

    # -- introspection ------------------------------------------------------------------

    @property
    def state(self) -> BrowserState:
        return self._state

    @property
    def active_pages(self) -> int:
        return sum(g.active for g in self._gens)

    # -- launching ----------------------------------------------------------------------

    def _plans(self) -> list[LaunchPlan]:
        s = self._settings()
        args = tuple(s.browser_args)
        plans: list[LaunchPlan] = []
        if s.browser_executable:
            plans.append(LaunchPlan("executable", s.browser_executable, args))
        elif s.browser_channel:
            plans.append(LaunchPlan("channel", s.browser_channel, (*args, "--headless=new")))
        plans.append(LaunchPlan("bundled", None, args))
        return plans

    async def _launch(self) -> tuple[Any, LaunchPlan]:
        errors: list[str] = []
        for n, plan in enumerate(self._plans()):
            label = f"{plan.source}:{plan.value or 'chromium'}"
            try:
                try:
                    browser = await self._launcher(plan)
                except Exception as exc:
                    if plan.source == "bundled" and _missing_executable(exc):
                        if not await self._installer():
                            raise
                        browser = await self._launcher(plan)
                    else:
                        raise
            except Exception as exc:
                errors.append(f"{label}: {str(exc).splitlines()[0] if str(exc) else exc!r}"[:300])
                log.warning("browser_launch_failed", plan=label, error=errors[-1])
                continue
            if n:
                self.stats.fallbacks += 1
                log.warning("browser_fallback", using=label, failed=errors)
            self.stats.plans.append(label)
            return browser, plan
        raise BrowserUnavailable("no browser could be started: " + "; ".join(errors))

    def _connected(self, gen: _Generation) -> bool:
        probe = getattr(gen.browser, "is_connected", None)
        return bool(probe()) if callable(probe) else True

    async def _current(self) -> _Generation:
        async with self._lock:
            if self._closed:
                raise BrowserUnavailable("the engine is shutting down")
            gen = self._current_gen
            if gen is not None and not gen.retired and self._connected(gen):
                return gen
            if gen is not None:
                await self._retire(gen, "disconnected")
            now = self._clock.monotonic()
            if now < self._unavailable_until:
                raise BrowserUnavailable(self._unavailable_reason)
            try:
                browser, plan = await self._launch()
            except BrowserUnavailable as exc:
                self._unavailable_until = now + UNAVAILABLE_RETRY_S
                self._unavailable_reason = str(exc)
                self._state = "unavailable"
                raise
            gen = _Generation(browser, plan)
            self._current_gen = gen
            self._gens.add(gen)
            self._state = "running"
            self._unavailable_until = 0.0
            self.stats.launches += 1
            self._last_used = self._clock.monotonic()
            self._ensure_idle_task()
            return gen

    async def _context(self, gen: _Generation, verify_tls: bool) -> Any:
        async with gen.lock:
            ctx = gen.contexts.get(verify_tls)
            if ctx is None:
                s = self._settings()
                opts: dict[str, Any] = {
                    "viewport": dict(VIEWPORT),
                    "device_scale_factor": 1,
                    "user_agent": s.default_user_agent,
                    "ignore_https_errors": not verify_tls,
                    "accept_downloads": False,
                    "service_workers": "block",
                    "locale": "en-US",
                }
                if s.global_proxy:
                    opts["proxy"] = {"server": s.global_proxy}
                ctx = await gen.browser.new_context(**opts)
                gen.contexts[verify_tls] = ctx
            return ctx

    async def ensure(self) -> None:
        """Start the browser if it is not running. Kept apart from the per-page timeout: a first
        launch may download Chromium, which must not count against a page's 45 s."""
        await self._current()

    # -- pages --------------------------------------------------------------------------

    def _semaphore(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(1, min(MAX_PAGES, self._settings().browser_pool)))
        return self._slots

    @asynccontextmanager
    async def page(
        self,
        *,
        verify_tls: bool = True,
        headers: dict[str, str] | None = None,
        block_media: bool = True,
    ) -> AsyncIterator[Any]:
        """A fresh page in the shared context, closed afterwards. At most 3 at once."""
        async with self._semaphore():
            gen = await self._current()
            gen.active += 1
            gen.pages += 1
            self.stats.pages_served += 1
            page: Any = None
            try:
                ctx = await self._context(gen, verify_tls)
                page = await ctx.new_page()
                if headers:
                    await page.set_extra_http_headers(headers)
                if block_media:
                    await page.route("**/*", _block_media)
                yield page
            finally:
                if page is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(page.close(), 5.0)
                gen.active -= 1
                self._last_used = self._clock.monotonic()
                await self._after_page(gen)

    async def _after_page(self, gen: _Generation) -> None:
        async with self._lock:
            if gen is self._current_gen and gen.pages >= RECYCLE_AFTER_PAGES:
                await self._retire(gen, "pages")
            elif gen.retired and gen.active == 0:
                await self._close_gen(gen)

    async def recycle(self, reason: str) -> bool:
        """Retire the running browser (the memory guard's first remedy): it closes now if no page
        is using it, otherwise as soon as its pages finish; the next page launches a fresh one.
        Returns False when no browser was running."""
        async with self._lock:
            gen = self._current_gen
            if gen is None:
                return False
            await self._retire(gen, reason)
            return True

    def note_crash(self) -> None:
        """A page died because the browser did: three in a row recycle the browser."""
        self._crashes += 1
        gen = self._current_gen
        if self._crashes >= RECYCLE_AFTER_CRASHES and gen is not None:
            self._crashes = 0
            gen.retired = True
            self._current_gen = None
            self.stats.recycles += 1
            log.warning("browser_recycle", reason="crashes")
            task = asyncio.create_task(self._reap(), name="browser-reap")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _reap(self) -> None:
        """Close retired browsers that nothing is using any more."""
        async with self._lock:
            for gen in list(self._gens):
                if gen.retired and gen.active == 0:
                    await self._close_gen(gen)

    def note_success(self) -> None:
        self._crashes = 0

    async def _retire(self, gen: _Generation, reason: str) -> None:
        """Stop handing out ``gen``; close it now if nothing is using it, else when it drains."""
        if not gen.retired:
            gen.retired = True
            self.stats.recycles += 1
            log.info("browser_recycle", reason=reason)
        if self._current_gen is gen:
            self._current_gen = None
        if gen.active == 0:
            await self._close_gen(gen)

    async def _close_gen(self, gen: _Generation) -> None:
        self._gens.discard(gen)
        if self._current_gen is gen:
            self._current_gen = None
        for ctx in list(gen.contexts.values()):
            with suppress(Exception):
                await asyncio.wait_for(ctx.close(), 5.0)
        gen.contexts.clear()
        with suppress(Exception):
            await asyncio.wait_for(gen.browser.close(), 10.0)
        if not self._gens and not self._closed and self._state == "running":
            self._state = "stopped"

    # -- idle close ---------------------------------------------------------------------

    def _ensure_idle_task(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_loop(), name="browser-idle")

    async def _idle_loop(self) -> None:
        while True:
            await self._clock.sleep(IDLE_CHECK_S)
            if not self._gens:
                return
            if self.active_pages == 0 and self._clock.monotonic() - self._last_used >= IDLE_CLOSE_S:
                async with self._lock:
                    if self.active_pages == 0:
                        for gen in list(self._gens):
                            await self._close_gen(gen)
                        self._current_gen = None
                        self._state = "stopped"
                        self.stats.idle_closes += 1
                        log.info("browser_idle_close")
                return

    async def close(self) -> None:
        self._closed = True
        task, self._idle_task = self._idle_task, None
        pending = [t for t in (task, *self._tasks) if t is not None]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for gen in list(self._gens):
            await self._close_gen(gen)
        self._current_gen = None
        self._state = "stopped"
        closer = getattr(self._launcher, "aclose", None)
        if closer is not None:
            await closer()


async def _block_media(route: Any) -> None:
    if route.request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


# -- errors -----------------------------------------------------------------------------

_DNS = ("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED")
_TLS = ("ERR_CERT", "ERR_SSL", "ERR_TLS", "SSL_ERROR")
_CONNECTION = (
    "ERR_CONNECTION", "ERR_ADDRESS_UNREACHABLE", "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED", "ERR_TIMED_OUT", "ERR_EMPTY_RESPONSE", "ERR_PROXY",
)  # fmt: skip
_CRASH = (
    "Target closed", "Target page, context or browser has been closed",
    "Browser has been closed", "has been closed", "Connection closed", "browser has disconnected",
    "Page crashed", "crashed",
)  # fmt: skip


def classify_browser_error(exc: BaseException) -> tuple[FetchError, bool]:
    """Map a Playwright failure to a ``FetchError``; the flag says the *browser* died."""
    text = str(exc)
    name = type(exc).__name__
    if isinstance(exc, TimeoutError | asyncio.TimeoutError) or name == "TimeoutError":
        return FetchError(FetchErrorKind.TIMEOUT, "timed out"), False
    if any(m in text for m in _DNS):
        return FetchError(FetchErrorKind.DNS, text.splitlines()[0][:300]), False
    if any(m in text for m in _TLS):
        return FetchError(FetchErrorKind.TLS, text.splitlines()[0][:300]), False
    if any(m in text for m in _CONNECTION):
        return FetchError(FetchErrorKind.CONNECTION, text.splitlines()[0][:300]), False
    if "Download is starting" in text:
        return FetchError(
            FetchErrorKind.BROWSER,
            "the URL is a file download, not a page; use the static method for it",
        ), False
    crashed = any(m in text for m in _CRASH)
    first = text.splitlines()[0] if text else name
    return FetchError(FetchErrorKind.BROWSER, f"{name}: {first}"[:300]), crashed


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        p = urlsplit(url)
        return (p.scheme, (p.hostname or "").lower(), p.port)
    except ValueError:
        return None


_INLINE_IFRAME_JS = """(el, html) => {
  const d = document.createElement('div');
  d.setAttribute('data-pw-iframe', el.getAttribute('src') || '');
  d.innerHTML = html;
  el.replaceWith(d);
}"""


async def inline_iframes(page: Any) -> int:
    """Replace every same-origin iframe by a ``div`` holding its body, so its text is part of the
    page's DOM (the extractor drops iframes). Deepest frames first. Returns how many."""
    main = page.main_frame
    base = _origin(page.url)
    done = 0
    for frame in reversed([f for f in page.frames if f != main]):
        url = frame.url or ""
        if not (url.startswith("about:") or (base is not None and _origin(url) == base)):
            continue
        try:
            inner = await frame.evaluate("document.body ? document.body.innerHTML : ''")
            handle = await frame.frame_element()
            await handle.evaluate(_INLINE_IFRAME_JS, inner)
            done += 1
        except Exception:  # the frame navigated away or was detached meanwhile
            continue
    return done


async def interact(page: Any, opts: BrowserOptions) -> None:
    """The waits and inputs that let a page finish building itself."""
    if opts.delay_after_load_s:
        await asyncio.sleep(opts.delay_after_load_s)
    for i in range(opts.mouse_moves):
        await page.mouse.move(120 + (i * 137) % 900, 140 + (i * 89) % 600, steps=4)
    for key in opts.keys:
        await page.keyboard.press(key)
    for _ in range(opts.scroll_count):
        await page.mouse.wheel(0, SCROLL_PX)
        await asyncio.sleep(SCROLL_GAP_S)


class BrowserFetcher:
    """Renders the page and returns its serialised DOM (``body``)."""

    screenshot = False

    def __init__(self, manager: BrowserManager) -> None:
        self._manager = manager

    async def aclose(self) -> None:
        return None  # the manager is owned (and closed) by the engine

    async def fetch(self, request: FetchRequest) -> FetchResult:
        started = time.perf_counter()
        try:
            await self._manager.ensure()
            async with asyncio.timeout(PAGE_TIMEOUT_S + HARD_GRACE_S):
                result = await self._fetch(request)
        except BrowserUnavailable as exc:
            return self._fail(request, started, FetchError(FetchErrorKind.BROWSER, str(exc)[:300]))
        except Exception as exc:
            err, crashed = classify_browser_error(exc)
            if crashed:
                self._manager.note_crash()
            return self._fail(request, started, err)
        self._manager.note_success()
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _fail(request: FetchRequest, started: float, err: FetchError) -> FetchResult:
        elapsed = int((time.perf_counter() - started) * 1000)
        return FetchResult(request.url, None, {}, "", b"", elapsed_ms=elapsed, error=err)

    async def _fetch(self, request: FetchRequest) -> FetchResult:
        cfg = request.resolved.fetch
        opts = cfg.browser
        headers = dict(cfg.headers)
        if cfg.user_agent:
            headers["User-Agent"] = cfg.user_agent
        async with self._manager.page(
            verify_tls=cfg.verify_tls, headers=headers or None, block_media=not self.screenshot
        ) as page:
            response = await page.goto(
                request.url, wait_until="load", timeout=PAGE_TIMEOUT_S * 1000
            )
            status = response.status if response is not None else None
            rheaders = (
                {k.lower(): v for k, v in (await response.all_headers()).items()}
                if response is not None
                else {}
            )
            if status is not None and status >= 400:
                err = FetchError(
                    FetchErrorKind.HTTP, f"HTTP {status}", status=status,
                    retry_after_s=parse_retry_after(rheaders.get("retry-after")),
                )  # fmt: skip
                return FetchResult(page.url, status, rheaders, rheaders.get("content-type", ""),
                                   b"", error=err)  # fmt: skip
            await interact(page, opts)
            await inline_iframes(page)
            png = await self._capture(page, opts) if self.screenshot else None
            html = await page.content()
            body = html.encode("utf-8")
            if len(body) > cfg.max_bytes:
                err = FetchError(FetchErrorKind.TOO_LARGE, f"DOM exceeds {cfg.max_bytes} bytes")
                return FetchResult(page.url, status, rheaders, "", b"", error=err)
            return FetchResult(
                final_url=page.url,
                status=status,
                headers=rheaders,
                content_type="text/html; charset=utf-8",
                body=body,
                screenshot_png=png,
            )

    async def _capture(self, page: Any, opts: BrowserOptions) -> bytes:
        """A PNG at the fixed viewport, device scale 1, animations and caret off."""
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=5000)
        await page.add_style_tag(content=NO_MOTION_CSS)
        await page.evaluate("window.scrollTo(0, 0)")
        kwargs: dict[str, Any] = {
            "type": "png", "animations": "disabled", "caret": "hide", "scale": "css",
            "full_page": opts.full_page or opts.clip is not None,
        }  # fmt: skip
        if opts.clip is not None:
            c = opts.clip
            kwargs["clip"] = {"x": c.x, "y": c.y, "width": c.w, "height": c.h}
        shot: bytes = await page.screenshot(**kwargs)
        return shot
