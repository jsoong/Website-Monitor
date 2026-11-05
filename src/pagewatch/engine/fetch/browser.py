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
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pagewatch.engine.clock import Clock
from pagewatch.engine.fetch import cookies as cookie_files
from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.fetch.login import MacroPermit, signal_in_page
from pagewatch.engine.fetch.macro import MacroError, run_macro
from pagewatch.engine.fetch.proxy import ProxyError, resolve_proxy
from pagewatch.engine.fetch.static import is_local_host, parse_retry_after
from pagewatch.engine.logs import get_logger
from pagewatch.engine.secrets import NullSecrets, SecretStore
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

# Chromium drops session cookies (no expiry) when a persistent profile closes, and many portals
# keep their login in exactly such a cookie. So the cookies of a profile are saved beside it
# when its browser closes and put back when the next one starts (found by a real-browser test).
SESSION_COOKIES = "pagewatch-session.json"


class BrowserUnavailable(RuntimeError):
    """No browser could be started (Edge and the bundled Chromium both failed)."""


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    source: Literal["executable", "channel", "bundled"]
    value: str | None
    args: tuple[str, ...] = ()


Launcher = Callable[[LaunchPlan], Awaitable[Any]]
Installer = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """A check that must not share the browser's ephemeral context: it has a profile of its own
    (a sign-in kept between checks), cookies, a proxy or a server login that belong to one
    bookmark. With ``profile_dir`` the context is persistent and is its own browser process
    (Playwright cannot make a persistent context inside a running browser); without it, the
    context is a fresh one in the shared browser, closed with the page."""

    profile_dir: Path | None = None
    proxy: dict[str, str] | None = None
    http_credentials: dict[str, str] | None = None
    cookies: tuple[dict[str, Any], ...] = ()


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

    async def launch_persistent(
        self, plan: LaunchPlan, user_data_dir: str, *, headless: bool, **options: Any
    ) -> Any:
        """A browser that keeps its cookies and storage in ``user_data_dir`` (a sign-in profile).
        The sign-in window (``headless=False``) and the checks (new headless mode) use the same
        browser, so the checking browser looks like the one that signed in."""
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        args = [a for a in plan.args if headless or not a.startswith("--headless")]
        kwargs: dict[str, Any] = {"headless": headless, "args": args, **options}
        if plan.source == "executable":
            kwargs["executable_path"] = plan.value
        elif plan.source == "channel":
            kwargs["channel"] = plan.value
        return await self._pw.chromium.launch_persistent_context(user_data_dir, **kwargs)

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


async def save_session_cookies(ctx: Any, profile_dir: Path) -> bool:
    """Write the context's cookies, session ones included, to the profile. Best effort: a
    browser that is already gone has nothing to give."""
    try:
        cookies = await asyncio.wait_for(ctx.cookies(), 5.0)
    except Exception:
        return False
    target = profile_dir / SESSION_COOKIES

    def write() -> None:
        tmp = target.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cookies, fh)
        os.replace(tmp, target)

    try:
        await asyncio.to_thread(write)
    except OSError:
        return False
    return True


async def restore_session_cookies(ctx: Any, profile_dir: Path) -> int:
    """Put the saved cookies back into a freshly started profile. Returns how many."""
    source = profile_dir / SESSION_COOKIES

    def read() -> list[dict[str, Any]]:
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

    cookies = await asyncio.to_thread(read)
    if not cookies:
        return 0
    try:
        await ctx.add_cookies(cookies)
    except Exception:
        log.warning("session_cookies_not_restored", profile=profile_dir.name)
        return 0
    return len(cookies)


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
        self._persistent: dict[Any, Path] = {}  # sign-in profiles open right now -> their folder
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

    async def _launch(
        self, launch: Callable[[LaunchPlan], Awaitable[Any]] | None = None
    ) -> tuple[Any, LaunchPlan]:
        """Start the first browser that works: Edge, else the bundled Chromium."""
        start = launch or self._launcher
        errors: list[str] = []
        for n, plan in enumerate(self._plans()):
            label = f"{plan.source}:{plan.value or 'chromium'}"
            try:
                try:
                    browser = await start(plan)
                except Exception as exc:
                    if plan.source == "bundled" and _missing_executable(exc):
                        if not await self._installer():
                            raise
                        browser = await start(plan)
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

    def _context_options(self, verify_tls: bool) -> dict[str, Any]:
        s = self._settings()
        return {
            "viewport": dict(VIEWPORT),
            "device_scale_factor": 1,
            "user_agent": s.default_user_agent,
            "ignore_https_errors": not verify_tls,
            "accept_downloads": False,
            "service_workers": "block",
            "locale": "en-US",
        }

    async def _context(self, gen: _Generation, verify_tls: bool) -> Any:
        async with gen.lock:
            ctx = gen.contexts.get(verify_tls)
            if ctx is None:
                opts = self._context_options(verify_tls)
                s = self._settings()
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
        session: SessionSpec | None = None,
    ) -> AsyncIterator[Any]:
        """A fresh page in the shared context, closed afterwards. At most 3 at once. With a
        ``session`` the page lives in a context of its own (see ``SessionSpec``)."""
        if session is not None:
            async with self._session_page(session, verify_tls, headers, block_media) as own:
                yield own
            return
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

    async def launch_profile(
        self, profile_dir: Path, *, headless: bool, verify_tls: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> tuple[Any, LaunchPlan]:  # fmt: skip
        """A persistent browser context on ``profile_dir``. Used for checks (headless) and for the
        owner's sign-in window (visible). The caller closes the context."""
        fn = getattr(self._launcher, "launch_persistent", None)
        if fn is None:
            raise BrowserUnavailable("this browser launcher cannot keep a sign-in profile")
        if self._closed:
            raise BrowserUnavailable("the engine is shutting down")
        await asyncio.to_thread(profile_dir.mkdir, parents=True, exist_ok=True)
        opts = {**self._context_options(verify_tls), **(extra or {})}
        ctx, plan = await self._launch(
            lambda plan: fn(plan, str(profile_dir), headless=headless, **opts)
        )
        self._persistent[ctx] = profile_dir
        await restore_session_cookies(ctx, profile_dir)
        return ctx, plan

    async def release_profile(self, ctx: Any) -> None:
        """Save the profile's cookies, then close its browser."""
        profile_dir = self._persistent.pop(ctx, None)
        if profile_dir is not None:
            await save_session_cookies(ctx, profile_dir)
        with suppress(Exception):
            await asyncio.wait_for(ctx.close(), 10.0)

    @asynccontextmanager
    async def _session_page(
        self, session: SessionSpec, verify_tls: bool, headers: dict[str, str] | None,
        block_media: bool,
    ) -> AsyncIterator[Any]:  # fmt: skip
        async with self._semaphore():
            extra: dict[str, Any] = {}
            if session.proxy:
                extra["proxy"] = session.proxy
            if session.http_credentials:
                extra["http_credentials"] = session.http_credentials
            ctx: Any = None
            page: Any = None
            gen: _Generation | None = None
            persistent = session.profile_dir is not None
            try:
                if session.profile_dir is not None:
                    ctx, _ = await self.launch_profile(
                        session.profile_dir, headless=True, verify_tls=verify_tls, extra=extra
                    )
                else:
                    gen = await self._current()
                    gen.active += 1
                    gen.pages += 1
                    opts = {**self._context_options(verify_tls), **extra}
                    ctx = await gen.browser.new_context(**opts)
                self.stats.pages_served += 1
                if session.cookies:
                    await ctx.add_cookies(list(session.cookies))
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
                if ctx is not None:
                    if persistent:
                        await self.release_profile(ctx)
                    else:
                        with suppress(Exception):
                            await asyncio.wait_for(ctx.close(), 5.0)
                if gen is not None:
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
        for ctx in list(self._persistent):
            await self.release_profile(ctx)
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


class _ConfigProblem(Exception):
    """The bookmark's own settings cannot be used (a missing secret, a refused password)."""


class BrowserFetcher:
    """Renders the page and returns its serialised DOM (``body``)."""

    screenshot = False

    def __init__(self, manager: BrowserManager, secrets: SecretStore | None = None) -> None:
        self._manager = manager
        self._secrets: SecretStore = secrets or NullSecrets()

    def use_secrets(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    async def aclose(self) -> None:
        return None  # the manager is owned (and closed) by the engine

    async def fetch(self, request: FetchRequest) -> FetchResult:
        started = time.perf_counter()
        try:
            session = await self._session(request)
            await self._manager.ensure()
            async with asyncio.timeout(PAGE_TIMEOUT_S + HARD_GRACE_S):
                result = await self._fetch(request, session)
        except _ConfigProblem as exc:
            return self._fail(request, started, FetchError(FetchErrorKind.PARSE, str(exc)[:300]))
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

    async def _session(self, request: FetchRequest) -> SessionSpec | None:
        """What this bookmark needs beyond the shared context: its own profile, cookies, proxy
        or server login. ``None`` for an ordinary page (the shared context serves it)."""
        cfg = request.resolved.fetch
        plan = request.login
        try:
            proxy = await resolve_proxy(cfg, request.settings, self._secrets)
        except ProxyError as exc:
            raise _ConfigProblem(str(exc)) from None
        own_proxy = proxy is not None and (cfg.proxy is not None or proxy.username is not None)
        profile = plan.profile_dir if plan is not None else None
        if not (profile or cfg.cookies_file or cfg.auth or own_proxy):
            return None
        if proxy is not None and proxy.is_socks and proxy.username:
            raise _ConfigProblem(
                "the browser cannot sign in to a SOCKS5 proxy; use an http:// proxy for browser "
                "checks, or a SOCKS5 proxy without a login"
            )
        credentials: dict[str, str] | None = None
        if cfg.auth is not None:
            password = await asyncio.to_thread(self._secrets.get, cfg.auth.secret_key)
            if password is None:
                raise _ConfigProblem(
                    f"no stored secret named {cfg.auth.secret_key!r} for the login"
                )
            parts = urlsplit(request.url)
            if (
                cfg.auth.kind == "basic"
                and parts.scheme == "http"
                and not is_local_host(parts.hostname)
            ):
                raise _ConfigProblem(
                    "refusing to send a Basic password over plain http; use https:// or Digest"
                )
            credentials = {"username": cfg.auth.username, "password": password}
        cookies: tuple[dict[str, Any], ...] = ()
        if cfg.cookies_file:
            try:
                parsed = await asyncio.to_thread(cookie_files.load_cookie_file, cfg.cookies_file)
            except cookie_files.CookieFileError as exc:
                raise _ConfigProblem(f"{exc}; import it again") from None
            cookies = tuple(cookie_files.to_playwright(parsed.cookies, time.time()))
        return SessionSpec(
            profile_dir=profile,
            proxy=proxy.playwright() if (proxy is not None and own_proxy) else None,
            http_credentials=credentials,
            cookies=cookies,
        )

    @staticmethod
    def _login_result(
        page: Any, status: int | None, reason: str, macro_error: str | None = None
    ) -> FetchResult:
        return FetchResult(
            page.url, status, {}, "", b"", login_required=True, login_reason=reason,
            macro_error=macro_error,
        )  # fmt: skip

    async def _log_in(
        self, page: Any, request: FetchRequest, response: Any
    ) -> tuple[FetchResult | None, bool, Any]:
        """The sign-out check (spec: Session reuse). Returns ``(result, macro_ran, response)``.
        A result means the check ends here: the page is a login page nobody could get past.
        After a macro the bookmark's address is loaded again and *that* response is returned."""
        plan = request.login
        status = response.status if response is not None else None
        if plan is None or plan.signal is None:
            return None, False, response
        if await signal_in_page(page, plan.signal) is None:
            return None, False, response  # still signed in: the macro is skipped
        if plan.macro is None:
            return self._login_result(page, status, "login_signal"), False, response
        permit = await plan.permit_macro() if plan.permit_macro else MacroPermit(True)
        if not permit.allowed:
            return (
                self._login_result(page, status, permit.reason or "login_blocked"),
                False,
                response,
            )
        error: str | None
        try:
            await run_macro(page, plan.macro.steps, plan.resolve_secret)
            response = await page.goto(
                request.url, wait_until="load", timeout=PAGE_TIMEOUT_S * 1000
            )
            still = await signal_in_page(page, plan.signal)
            error = "still on the login page after the macro" if still else None
        except MacroError as exc:
            error = exc.message
        if plan.macro_finished is not None:
            await plan.macro_finished(error is None)
        if error is not None:
            return self._login_result(page, status, "macro_failed", error), False, response
        return None, True, response

    async def _fetch(self, request: FetchRequest, session: SessionSpec | None) -> FetchResult:
        cfg = request.resolved.fetch
        opts = cfg.browser
        headers = dict(cfg.headers)
        if cfg.user_agent:
            headers["User-Agent"] = cfg.user_agent
        async with self._manager.page(
            verify_tls=cfg.verify_tls, headers=headers or None, block_media=not self.screenshot,
            session=session,
        ) as page:  # fmt: skip
            response = await page.goto(
                request.url, wait_until="load", timeout=PAGE_TIMEOUT_S * 1000
            )
            gone, macro_ran, response = await self._log_in(page, request, response)
            if gone is not None:
                return gone
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
                macro_ran=macro_ran,
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
