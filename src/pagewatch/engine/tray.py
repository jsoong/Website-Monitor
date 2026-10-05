"""System-tray icon, owned by the engine so alerts and controls work with the UI closed.

``TrayController`` holds all the logic (which icon state, what the menu does) and is tested
anywhere; ``PystrayBackend`` is the thin Windows adapter, and ``NullTrayBackend`` stands in on
platforms without a tray (CI, Linux development).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from PIL import Image, ImageDraw

from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.tray")

REFRESH_S = 30.0
EVENTS_THAT_MATTER = {"bookmark_updated", "engine_state", "problem", "change_detected"}


class TrayState(StrEnum):
    NORMAL = "normal"
    UNREAD = "unread"
    PAUSED = "paused"
    OFFLINE = "offline"
    ERROR = "error"


STATE_COLOR = {
    TrayState.NORMAL: (90, 160, 90),
    TrayState.UNREAD: (230, 150, 20),
    TrayState.PAUSED: (140, 140, 140),
    TrayState.OFFLINE: (120, 120, 200),
    TrayState.ERROR: (210, 60, 60),
}


def compute_state(*, online: bool, paused: bool, errors: int, unread: int) -> TrayState:
    """Precedence: what stops monitoring wins over what merely needs attention."""
    if not online:
        return TrayState.OFFLINE
    if paused:
        return TrayState.PAUSED
    if errors:
        return TrayState.ERROR
    if unread:
        return TrayState.UNREAD
    return TrayState.NORMAL


def tooltip(state: TrayState, unread: int, errors: int) -> str:
    detail = {
        TrayState.NORMAL: "watching",
        TrayState.UNREAD: f"{unread} unread change{'s' if unread != 1 else ''}",
        TrayState.PAUSED: "AutoWatch paused",
        TrayState.OFFLINE: "offline: checks are waiting for the network",
        TrayState.ERROR: f"{errors} bookmark{'s' if errors != 1 else ''} failing",
    }[state]
    return f"PageWatch: {detail}"


def system_uses_dark_mode() -> bool:
    """Windows' app theme (the tray and the UI follow it); False elsewhere."""
    if sys.platform != "win32":
        return False
    try:  # pragma: no cover - needs Windows
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return int(winreg.QueryValueEx(key, "AppsUseLightTheme")[0]) == 0
    except OSError:
        return False


def make_icon(state: TrayState, *, dark: bool = False, size: int = 64) -> Image.Image:
    """A page glyph with a coloured status dot: readable on light and dark taskbars."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = (235, 235, 235, 255) if dark else (40, 40, 40, 255)
    s = size / 64
    d.rounded_rectangle(
        (12 * s, 6 * s, 48 * s, 58 * s), radius=5 * s, outline=ink, width=max(2, int(4 * s))
    )
    for y in (20, 30, 40):
        d.line((20 * s, y * s, 40 * s, y * s), fill=ink, width=max(1, int(3 * s)))
    r, g, b = STATE_COLOR[state]
    d.ellipse(
        (34 * s, 36 * s, 62 * s, 64 * s),
        fill=(r, g, b, 255),
        outline=(255, 255, 255, 255),
        width=max(1, int(2 * s)),
    )
    return img


class TrayBackend(Protocol):
    def set_state(self, state: TrayState, tip: str, dark: bool) -> None: ...

    def start(self, menu: list[tuple[str, Callable[[], None]]]) -> None: ...

    def stop(self) -> None: ...


class NullTrayBackend:
    """No tray available: remembers what it was told (tests, headless runs)."""

    def __init__(self) -> None:
        self.state: TrayState | None = None
        self.tip = ""
        self.history: list[TrayState] = []
        self.menu: list[tuple[str, Callable[[], None]]] = []
        self.started = False

    def set_state(self, state: TrayState, tip: str, dark: bool) -> None:
        self.state, self.tip = state, tip
        if not self.history or self.history[-1] is not state:
            self.history.append(state)

    def start(self, menu: list[tuple[str, Callable[[], None]]]) -> None:
        self.menu, self.started = menu, True

    def stop(self) -> None:
        self.started = False

    def click(self, label: str) -> None:
        dict(self.menu)[label]()


class PystrayBackend:  # pragma: no cover - needs a Windows desktop session
    def __init__(self) -> None:
        self._icon: Any = None

    def start(self, menu: list[tuple[str, Callable[[], None]]]) -> None:
        import pystray

        items = [pystray.MenuItem(label, lambda _i, _m, cb=cb: cb()) for label, cb in menu]
        self._icon = pystray.Icon(
            "PageWatch", make_icon(TrayState.NORMAL, dark=system_uses_dark_mode()), "PageWatch",
            pystray.Menu(*items),
        )  # fmt: skip
        self._icon.run_detached()  # its own thread

    def set_state(self, state: TrayState, tip: str, dark: bool) -> None:
        if self._icon is not None:
            self._icon.icon = make_icon(state, dark=dark)
            self._icon.title = tip

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()


def default_backend() -> TrayBackend:
    if sys.platform == "win32":  # pragma: no cover
        try:
            import pystray  # noqa: F401

            return PystrayBackend()
        except Exception:
            log.warning("tray_unavailable", exc_info=True)
    return NullTrayBackend()


class TrayController:
    def __init__(
        self,
        engine: Engine,
        backend: TrayBackend | None = None,
        launch_ui: Callable[[], None] | None = None,
        dark: Callable[[], bool] = system_uses_dark_mode,
    ) -> None:
        self.e = engine
        self.backend = backend or default_backend()
        self._launch = launch_ui or self._spawn_ui
        self._dark = dark
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._poke = asyncio.Event()
        self.state: TrayState = TrayState.NORMAL

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.backend.start(self.menu())
        self._task = asyncio.create_task(self._run(), name="tray")
        await self.refresh()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.backend.stop()

    async def _run(self) -> None:
        sub = self.e.events.subscribe()
        try:
            while True:
                getter = asyncio.ensure_future(sub.queue.get())
                sleeper = asyncio.ensure_future(self.e.clock.sleep(REFRESH_S))
                done, pending = await asyncio.wait(
                    {getter, sleeper}, return_when=asyncio.FIRST_COMPLETED
                )
                for p in pending:
                    p.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if getter in done and getter.result().type not in EVENTS_THAT_MATTER:
                    continue
                # coalesce a burst of events into one refresh
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                await self.refresh()
        finally:
            sub.close()

    # -- state --------------------------------------------------------------------------

    async def refresh(self) -> TrayState:
        def counts(conn: Any) -> tuple[int, int]:
            row = conn.execute(
                "SELECT COALESCE(SUM(unread),0), COALESCE(SUM(status='error'),0) FROM bookmark"
            ).fetchone()
            return int(row[0]), int(row[1])

        unread, errors = await self.e.db.read(counts)
        self.state = compute_state(
            online=self.e.online, paused=self.e.scheduler.paused, errors=errors, unread=unread
        )
        self.backend.set_state(self.state, tooltip(self.state, unread, errors), self._dark())
        return self.state

    # -- menu ---------------------------------------------------------------------------

    def menu(self) -> list[tuple[str, Callable[[], None]]]:
        return [
            ("Open PageWatch", lambda: self._soon(self.open_ui)),
            ("Check all now", lambda: self._soon(self.check_all)),
            ("Pause AutoWatch for 1 hour", lambda: self._soon(self.pause_hour)),
            ("Pause AutoWatch until resumed", lambda: self._soon(self.pause_forever)),
            ("Resume AutoWatch", lambda: self._soon(self.resume)),
            ("Quit engine", lambda: self._soon(self.quit)),
        ]

    def _soon(self, coro_fn: Callable[[], Any]) -> None:
        """Menu callbacks run on the tray's own thread: hop onto the engine's loop."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: self.e.spawn(coro_fn(), "tray-action"))

    async def open_ui(self) -> None:
        self._launch()

    async def check_all(self) -> None:
        ids = await self.e.db.read(repo.bookmark_all_ids)
        self.e.check_now(ids)

    async def pause_hour(self) -> None:
        await self.e.set_autowatch("paused", self.e.clock.now() + timedelta(hours=1))
        await self.refresh()

    async def pause_forever(self) -> None:
        await self.e.set_autowatch("paused", None)
        await self.refresh()

    async def resume(self) -> None:
        await self.e.set_autowatch("running", None)
        await self.refresh()

    async def quit(self) -> None:
        self.e.request_stop()

    def _spawn_ui(self) -> None:  # pragma: no cover - launches a real process
        args = [sys.executable, "-m", "pagewatch.ui.main", "--data-dir", str(self.e.data_dir.root)]
        flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
        subprocess.Popen(args, creationflags=flags, close_fds=True)  # noqa: S603
