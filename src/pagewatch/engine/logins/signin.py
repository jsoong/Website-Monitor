"""Semi-attended sign-in (spec: Portal strategy).

"Sign in" opens a **visible** browser window on the bookmark's persistent profile. The owner logs
in by hand (one-time codes included); when the window closes, or the UI says "done", the
engine saves the session, leaves ``needs_login`` and **checks immediately**, while the session is
fresh. Nothing here ever types a password, and nothing pings a site to keep a session open.

Playwright is reached through ``SignInOpener`` so the logic is tested with a fake window (default
suite) and with a real Chromium in new headless mode (``-m browser``).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pagewatch.engine.clock import iso
from pagewatch.engine.config import resolve
from pagewatch.engine.fetch.browser import BrowserManager, save_session_cookies
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo
from pagewatch.models import SignInOut

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.signin")

SNAPSHOT_EVERY_S = 1.0  # cookies are copied out while the window is open (see _Window)


class SignInError(RuntimeError):
    """The sign-in window cannot be opened (not configured, already open, no browser)."""


class SignInWindow(Protocol):
    async def wait_closed(self) -> None: ...

    async def close(self) -> None: ...


class SignInOpener(Protocol):
    async def open(self, profile_dir: Path, url: str) -> SignInWindow: ...


class _Window:
    """A Playwright persistent context the owner is using."""

    def __init__(self, manager: BrowserManager, ctx: Any, page: Any, profile_dir: Path) -> None:
        self._manager = manager
        self.ctx = ctx
        self.page = page  # tests drive it; the owner uses the real window
        self._profile = profile_dir
        self._closed = asyncio.Event()
        ctx.on("close", lambda *_: self._closed.set())
        # A page that closes is the last moment the browser may still answer for its cookies.
        page.on("close", lambda *_: self._snapshot_soon())
        self._background: set[asyncio.Task[Any]] = set()
        # Closing the window ends the browser, and a closed browser cannot hand over its session
        # cookies; so they are copied out while it is open. At most a second of them is at risk.
        self._snapshots = asyncio.create_task(self._snapshot_loop(), name="signin-cookies")

    def _snapshot_soon(self) -> None:
        task = asyncio.create_task(save_session_cookies(self.ctx, self._profile))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _snapshot_loop(self) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(SNAPSHOT_EVERY_S)
            await save_session_cookies(self.ctx, self._profile)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._snapshots.cancel()
        await asyncio.gather(self._snapshots, return_exceptions=True)
        await self._manager.release_profile(self.ctx)  # saves the cookies, then closes


class PlaywrightSignInOpener:
    def __init__(self, manager: BrowserManager, *, headless: bool = False) -> None:
        self._manager = manager
        self.headless = headless  # tests only: a window nobody can see

    async def open(self, profile_dir: Path, url: str) -> _Window:
        ctx, _plan = await self._manager.launch_profile(profile_dir, headless=self.headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        with suppress(Exception):  # an unreachable site must not stop the owner from trying
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return _Window(self._manager, ctx, page, profile_dir)


@dataclass(slots=True)
class _Open:
    window: SignInWindow
    watcher: asyncio.Task[None]


class SignInService:
    def __init__(self, engine: Engine, opener: SignInOpener | None = None) -> None:
        self.e = engine
        self._opener = opener
        self._open: dict[int, _Open] = {}
        self._closing: set[int] = set()

    def set_opener(self, opener: SignInOpener) -> None:
        self._opener = opener

    @property
    def opener(self) -> SignInOpener:
        if self._opener is None:
            self._opener = PlaywrightSignInOpener(self.e.browser)
        return self._opener

    def is_open(self, bookmark_id: int) -> bool:
        return bookmark_id in self._open

    def window(self, bookmark_id: int) -> SignInWindow | None:
        held = self._open.get(bookmark_id)
        return held.window if held else None

    async def start(self, bookmark_id: int) -> SignInOut:
        row = await self.e.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        if row is None:
            raise SignInError(f"bookmark {bookmark_id} not found")
        cfg = resolve(row, self.e.folders, self.e.settings).fetch
        if cfg.login is None or not cfg.login.semi_attended:
            raise SignInError("this bookmark is not set up for semi-attended sign-in")
        if bookmark_id in self._open:
            raise SignInError("a sign-in window is already open for this bookmark")
        if self.e.scheduler.is_in_flight(bookmark_id):
            raise SignInError("a check of this bookmark is running; try again in a moment")
        url = cfg.login.signin_url or row["url"]
        try:
            window = await self.opener.open(self.e.logins.profile_dir(bookmark_id), url)
        except Exception as exc:
            log.warning("signin_open_failed", bookmark_id=bookmark_id, error=type(exc).__name__)
            raise SignInError(f"the sign-in window could not be opened: {exc}"[:300]) from exc
        watcher = asyncio.create_task(
            self._watch(bookmark_id, window), name=f"signin-{bookmark_id}"
        )
        self._open[bookmark_id] = _Open(window, watcher)
        self.e.events.publish("signin", {"bookmark_id": bookmark_id, "state": "started"})
        return SignInOut(bookmark_id=bookmark_id, state="started")

    async def _watch(self, bookmark_id: int, window: SignInWindow) -> None:
        """The owner closed the window themselves: that is "done"."""
        await window.wait_closed()
        await self._finish(bookmark_id, close_window=False)

    async def finish(self, bookmark_id: int) -> SignInOut:
        if bookmark_id not in self._open:
            return SignInOut(bookmark_id=bookmark_id, state="idle", message="no window is open")
        await self._finish(bookmark_id, close_window=True)
        return SignInOut(bookmark_id=bookmark_id, state="finished")

    async def _finish(self, bookmark_id: int, *, close_window: bool) -> None:
        if bookmark_id in self._closing:
            return
        held = self._open.get(bookmark_id)
        if held is None:
            return
        self._closing.add(bookmark_id)
        try:
            if close_window:
                held.watcher.cancel()
            await held.window.close()
            self._open.pop(bookmark_id, None)
            now = iso(self.e.clock.now())
            await self.e.db.write(
                lambda c: repo.login_state_set(c, bookmark_id, last_signin_at=now)
            )
            self.e.events.publish("signin", {"bookmark_id": bookmark_id, "state": "finished"})
            # The session is fresh: leave needs_login and check now (spec: checks immediately).
            await self.e.logins.experiments.note_signin(bookmark_id)  # time zero, if one waits
            await self.e.logins.resume(bookmark_id, reset_macro_block=False)
        finally:
            self._closing.discard(bookmark_id)

    async def aclose(self) -> None:
        for bookmark_id, held in list(self._open.items()):
            held.watcher.cancel()
            with suppress(Exception):
                await held.window.close()
            self._open.pop(bookmark_id, None)
