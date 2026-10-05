"""Windows toasts, coalesced per window.

Changes detected within one ``toast_coalesce_s`` window produce one summary toast
("5 bookmarks changed"). A lone change gets its own toast with Open and Mark read buttons.
The backend is pluggable: ``WindowsToastBackend`` on Windows, ``LogToastBackend`` elsewhere.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from pagewatch.engine.clock import Clock
from pagewatch.engine.logs import get_logger

log = get_logger("pagewatch.toast")

ActivationHandler = Callable[[str, list[int]], Coroutine[Any, Any, None]]  # (action, bookmark ids)


@dataclass(slots=True)
class Toast:
    title: str
    body: str
    bookmark_ids: list[int]
    change_ids: list[int] = field(default_factory=list)
    buttons: bool = True


class ToastBackend(Protocol):
    async def show(self, toast: Toast) -> None: ...


class LogToastBackend:
    """Used where there is no Windows shell: records toasts in the log."""

    def __init__(self) -> None:
        self.shown: list[Toast] = []

    async def show(self, toast: Toast) -> None:
        self.shown.append(toast)
        log.info("toast", title=toast.title, bookmarks=toast.bookmark_ids)


class WindowsToastBackend:  # pragma: no cover - needs a Windows desktop session
    """windows-toasts with two buttons; activations go to the engine."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_activate: ActivationHandler) -> None:
        from windows_toasts import InteractableWindowsToaster

        self._toaster = InteractableWindowsToaster("PageWatch")
        self._loop = loop
        self._on_activate = on_activate

    async def show(self, toast: Toast) -> None:
        from windows_toasts import Toast as WinToast
        from windows_toasts import ToastActivatedEventArgs, ToastButton

        win = WinToast([toast.title, toast.body])
        if toast.buttons:
            win.AddAction(ToastButton("Open", "open"))
            win.AddAction(ToastButton("Mark read", "mark_read"))

        def activated(args: ToastActivatedEventArgs) -> None:
            action = args.arguments or "open"
            asyncio.run_coroutine_threadsafe(
                self._on_activate(action, toast.bookmark_ids), self._loop
            )

        win.on_activated = activated
        self._toaster.show_toast(win)


def default_backend(
    loop: asyncio.AbstractEventLoop, on_activate: ActivationHandler
) -> ToastBackend:
    if sys.platform == "win32":  # pragma: no cover
        try:
            return WindowsToastBackend(loop, on_activate)
        except Exception:
            log.warning("windows_toasts_unavailable", exc_info=True)
    return LogToastBackend()


class ToastService:
    def __init__(self, clock: Clock, backend: ToastBackend, window_s: Callable[[], float]) -> None:
        self._clock = clock
        self.backend = backend
        self._window_s = window_s
        self._pending: list[tuple[str, str, int, int | None]] = []
        self._waiters: list[asyncio.Future[None]] = []
        self._timer: asyncio.Task[None] | None = None

    async def notify(
        self, bookmark_id: int, name: str, body: str, change_id: int | None = None
    ) -> None:
        """Queue a toast; returns once the window it joined has been shown."""
        window = self._window_s()
        if window <= 0:
            await self.backend.show(
                Toast(name, body, [bookmark_id], [change_id] if change_id else [])
            )
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending.append((name, body, bookmark_id, change_id))
        self._waiters.append(fut)
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_after(window), name="toast-window")
        await fut

    async def _flush_after(self, window: float) -> None:
        await self._clock.sleep(window)
        pending, self._pending = self._pending, []
        waiters, self._waiters = self._waiters, []
        try:
            if len(pending) == 1:
                name, body, bid, cid = pending[0]
                await self.backend.show(Toast(name, body, [bid], [cid] if cid else []))
            elif pending:
                names = [p[0] for p in pending]
                shown = ", ".join(names[:3]) + (
                    f" and {len(names) - 3} more" if len(names) > 3 else ""
                )
                await self.backend.show(
                    Toast(
                        f"{len(pending)} bookmarks changed",
                        shown,
                        [p[2] for p in pending],
                        [p[3] for p in pending if p[3]],
                        buttons=False,
                    )
                )
        except Exception as exc:
            for w in waiters:
                if not w.done():
                    w.set_exception(exc)
        else:
            for w in waiters:
                if not w.done():
                    w.set_result(None)

    async def aclose(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
        for w in self._waiters:
            if not w.done():
                w.cancel()
