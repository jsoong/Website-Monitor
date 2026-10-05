"""Sleep, resume, connectivity and battery (spec: Scheduler -> Sleep, resume and connectivity).

* ``PowerMonitor`` notices that the machine slept (wall clock versus monotonic drift, a
  heartbeat that fires hours late, or a Windows power message), holds scheduled dispatch, waits
  for the network, then lets the engine catch up once.
* ``Connectivity`` probes a configurable URL. A probe that fails puts the engine in offline
  mode (dispatch paused, failures not counted) until one succeeds.
* Battery state is read once a minute and fed to the scheduler's per-bookmark policy.

Everything that needs Windows sits behind ``PowerBackend``; ``NullPowerBackend`` stands in for
tests and for platforms without it, so all of the logic runs (and is tested) anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

import httpx
import psutil

from pagewatch.engine.logs import get_logger

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.power")

HEARTBEAT_S = 5.0
RESUME_DEBOUNCE_S = 10.0  # a drift signal and an OS message for one wake are one resume

# What a backend reports through its callback.
EVENT_SUSPEND = "suspend"
EVENT_RESUME = "resume"
EVENT_SHUTDOWN = "shutdown"  # WM_QUERYENDSESSION: the user is logging off or shutting down


class PowerBackend(Protocol):
    def start(self, on_event: Callable[[str], None]) -> None:
        """Begin delivering ``suspend`` / ``resume`` / ``shutdown`` (from any thread)."""

    def stop(self) -> None: ...

    def on_battery(self) -> bool:
        """Running on battery power (a battery exists and the charger is not connected)."""

    def battery_saver(self) -> bool: ...

    def keep_awake(self, on: bool) -> None:
        """Ask the OS not to sleep while ``on`` (called from the engine's event-loop thread)."""


class NullPowerBackend:
    """No OS integration: state is whatever a test sets, calls are recorded."""

    def __init__(self) -> None:
        self.battery = False
        self.saver = False
        self.awake_calls: list[bool] = []
        self.started = False
        self._cb: Callable[[str], None] | None = None

    def start(self, on_event: Callable[[str], None]) -> None:
        self._cb, self.started = on_event, True

    def stop(self) -> None:
        self.started = False

    def on_battery(self) -> bool:
        return self.battery

    def battery_saver(self) -> bool:
        return self.saver

    def keep_awake(self, on: bool) -> None:
        self.awake_calls.append(on)

    def send(self, event: str) -> None:
        """Test control: deliver an OS power event as the Windows window would."""
        assert self._cb is not None, "backend not started"
        self._cb(event)


class SystemPowerBackend:
    """The real thing. Battery state comes from psutil everywhere (no battery = not on battery);
    battery saver, keep-awake and the power-message window are Windows-only and are a manual
    check (they need a real desktop session)."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._post: Callable[[int], object] | None = None

    def on_battery(self) -> bool:
        try:
            battery = psutil.sensors_battery()
        except Exception:
            return False
        return battery is not None and battery.power_plugged is False

    def battery_saver(self) -> bool:  # pragma: no cover - needs Windows
        if sys.platform != "win32":
            return False
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong),
            ]  # fmt: skip

        status = _Status()
        try:
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return False
        except Exception:
            return False
        return bool(status.SystemStatusFlag & 1)  # bit 0: battery saver is on

    def keep_awake(self, on: bool) -> None:  # pragma: no cover - needs Windows
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        try:  # per-thread state: the engine calls this from its (persistent) event-loop thread
            fn = ctypes.windll.kernel32.SetThreadExecutionState
            fn.argtypes = [wintypes.DWORD]
            fn.restype = wintypes.DWORD
            fn(es_continuous | (es_system_required if on else 0))
        except Exception:
            log.warning("keep_awake_failed", exc_info=True)

    # -- the hidden window --------------------------------------------------------------

    def start(self, on_event: Callable[[str], None]) -> None:  # pragma: no cover - Windows
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._window_main, args=(on_event,), name="pw-power-window", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:  # pragma: no cover - needs Windows
        if sys.platform != "win32" or self._hwnd is None or self._post is None:
            return
        with contextlib.suppress(Exception):
            self._post(self._hwnd)

    def _window_main(self, on_event: Callable[[str], None]) -> None:  # pragma: no cover
        """A hidden (never shown) top-level window: message-only windows do not receive the
        broadcast ``WM_POWERBROADCAST`` / ``WM_QUERYENDSESSION``. Any failure only costs the OS
        notification; the drift and heartbeat checks still find a resume.

        Every foreign function is given its argument and result types: without them ctypes
        assumes 32-bit ints, which truncates window handles on 64-bit Windows. This is the one
        part of M5 that could not be run in the development sandbox (see DECISIONS)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            lresult = ctypes.c_ssize_t
            wndproc_t = ctypes.WINFUNCTYPE(
                lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            )

            class WNDCLASSW(ctypes.Structure):  # ctypes.wintypes does not define it
                _fields_ = [
                    ("style", wintypes.UINT), ("lpfnWndProc", wndproc_t),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HANDLE), ("hIcon", wintypes.HANDLE),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ]  # fmt: skip

            msg_p = ctypes.POINTER(wintypes.MSG)
            user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.DefWindowProcW.restype = lresult
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HANDLE, wintypes.HANDLE, wintypes.LPVOID,
            ]  # fmt: skip
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.GetMessageW.argtypes = [msg_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.GetMessageW.restype = ctypes.c_int
            user32.TranslateMessage.argtypes = [msg_p]
            user32.DispatchMessageW.argtypes = [msg_p]
            user32.DispatchMessageW.restype = lresult
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            wm_power, wm_query_end, wm_close, wm_destroy = 0x0218, 0x0011, 0x0010, 0x0002
            suspend = {0x0004}  # PBT_APMSUSPEND
            resume = {0x0007, 0x0012, 0x0006}  # RESUMESUSPEND, RESUMEAUTOMATIC, RESUMECRITICAL

            def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
                try:
                    if msg == wm_power:
                        if wparam in suspend:
                            on_event(EVENT_SUSPEND)
                        elif wparam in resume:
                            on_event(EVENT_RESUME)
                        return 1  # TRUE: the broadcast is handled
                    if msg == wm_query_end:
                        on_event(EVENT_SHUTDOWN)
                        return 1  # TRUE: do not hold up the shutdown; the engine stops itself
                    if msg == wm_close:
                        user32.DestroyWindow(hwnd)
                        return 0
                    if msg == wm_destroy:
                        user32.PostQuitMessage(0)
                        return 0
                except Exception:
                    log.warning("power_window_callback_failed", exc_info=True)
                return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

            callback = wndproc_t(proc)  # must outlive the window
            instance = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = callback
            wc.hInstance = instance
            wc.lpszClassName = "PageWatchPowerWindow"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "PageWatch", 0, 0, 0, 0, 0, None, None, instance, None
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = int(hwnd)
            self._post = lambda handle: user32.PostMessageW(handle, wm_close, 0, 0)
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0 = quit, -1 = error
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.warning("power_window_failed", exc_info=True)


# -- connectivity -----------------------------------------------------------------------

ProbeFn = Callable[[str, float, str | None], Awaitable[bool]]


async def http_probe(url: str, timeout_s: float, proxy: str | None) -> bool:
    """HEAD ``url``. *Any* HTTP reply means the network works; only a failure to get one
    (DNS, refused, timeout, TLS) means offline. Goes through the global proxy, if any."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, proxy=proxy, follow_redirects=False
        ) as client:
            await client.head(url)
        return True
    except Exception:
        return False


class Connectivity:
    """Online/offline state machine for the engine."""

    def __init__(self, engine: Engine, probe: ProbeFn | None = None, cache_s: float = 2.0) -> None:
        self.e = engine
        self._probe_fn: ProbeFn = probe or http_probe
        self._cache_s = cache_s
        self._flight: asyncio.Future[bool] | None = None
        self._last: tuple[float, bool] | None = None
        self._recovery: asyncio.Task[None] | None = None
        self.probes = 0

    async def stop(self) -> None:
        task, self._recovery = self._recovery, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- probing ------------------------------------------------------------------------

    async def probe(self, *, fresh: bool = False) -> bool:
        """One probe, shared by everyone asking at the same time; the answer is reused for
        ``cache_s`` unless ``fresh``."""
        now = self.e.clock.monotonic()
        if not fresh and self._last is not None and now - self._last[0] < self._cache_s:
            return self._last[1]
        if self._flight is None:
            self._flight = asyncio.ensure_future(self._run_probe())
        return await asyncio.shield(self._flight)

    async def _run_probe(self) -> bool:
        s = self.e.settings
        self.probes += 1
        try:
            ok = bool(
                await self._probe_fn(s.connectivity_url, s.connectivity_timeout_s, s.global_proxy)
            )
        except Exception:
            ok = False
        self._last = (self.e.clock.monotonic(), ok)
        self._flight = None
        return ok

    async def verify(self) -> bool:
        """A check failed in a way that looks like a network problem: are we offline? Returns
        the engine's online state afterwards (and has already switched to offline mode if not)."""
        if not self.e.online:
            return False
        if await self.probe():
            return True
        self._set_online(False, "probe_failed")
        return self.e.online

    async def wait_for_network(self, window_s: float) -> bool:
        """After a resume: probe every ``probe_interval_s`` for up to ``window_s``. If the
        network never answers the engine goes offline (and keeps probing in the background)."""
        clock = self.e.clock
        deadline = clock.monotonic() + window_s
        while True:
            if await self.probe(fresh=True):
                self._set_online(True, "network_back", catch_up=False)
                return True
            left = deadline - clock.monotonic()
            if left <= 0:
                self._set_online(False, "no_network_after_resume")
                return False
            await clock.sleep(min(self.e.settings.probe_interval_s, left))

    # -- state --------------------------------------------------------------------------

    def go_offline(self, reason: str) -> None:
        self._set_online(False, reason)

    def _set_online(self, online: bool, reason: str, *, catch_up: bool = True) -> None:
        e = self.e
        if online:
            e.set_online(True, reason, catch_up=catch_up)
            return
        if e.online:
            e.set_online(False, reason)
        if self._recovery is None or self._recovery.done():
            self._recovery = asyncio.create_task(self._recover(), name="connectivity-recovery")

    async def _recover(self) -> None:
        """Offline mode: probe until the network answers, then resume and catch up."""
        while not self.e.online:
            await self.e.clock.sleep(self.e.settings.offline_probe_s)
            if not self.e.online and await self.probe(fresh=True):
                self._set_online(True, "network_back")


# -- the monitor ------------------------------------------------------------------------


class PowerMonitor:
    def __init__(
        self, engine: Engine, backend: PowerBackend | None = None, heartbeat_s: float = HEARTBEAT_S
    ) -> None:
        self.e = engine
        self.backend: PowerBackend = backend or SystemPowerBackend()
        self._heartbeat_s = heartbeat_s
        self._tasks: list[asyncio.Task[None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resuming = False
        self._last_resume = -1e9  # monotonic start of the last resume sequence
        self._suspended_wall: float | None = None
        self.resumes = 0

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.poll_battery()
        self.apply_keep_awake()
        if self.e.settings.os_power_events:  # off-switch for the native window (Windows only)
            self.backend.start(self._os_event)
        self._tasks = [
            asyncio.create_task(self._heartbeat(), name="power-heartbeat"),
            asyncio.create_task(self._battery_loop(), name="power-battery"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.backend.stop()
        with contextlib.suppress(Exception):
            self.backend.keep_awake(False)

    # -- resume detection ---------------------------------------------------------------

    async def _heartbeat(self) -> None:
        clock = self.e.clock
        wall0, mono0 = clock.now().timestamp(), clock.monotonic()
        while True:
            await clock.sleep(self._heartbeat_s)
            wall, mono = clock.now().timestamp(), clock.monotonic()
            drift = (wall - wall0) - (mono - mono0)  # the wall clock moved, monotonic did not
            late = (mono - mono0) - self._heartbeat_s  # monotonic that includes the suspend
            wall0, mono0 = wall, mono
            slept = max(drift, late)
            if slept > self.e.settings.resume_drift_s:
                await self.handle_resume(slept, "drift" if drift >= late else "heartbeat_late")
                # The sequence itself can take minutes (waiting for the network): that time is
                # not a second sleep, so measure from here.
                wall0, mono0 = clock.now().timestamp(), clock.monotonic()

    def _os_event(self, kind: str) -> None:
        """Called on the backend's thread."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._on_os_event, kind)

    def _on_os_event(self, kind: str) -> None:
        if kind == EVENT_SUSPEND:
            self._suspended_wall = self.e.clock.now().timestamp()
        elif kind == EVENT_RESUME:
            slept = (
                self.e.clock.now().timestamp() - self._suspended_wall
                if self._suspended_wall is not None
                else 0.0
            )
            self._suspended_wall = None
            self.e.spawn(self.handle_resume(slept, "os"), "resume")
        elif kind == EVENT_SHUTDOWN:
            log.info("session_ending")
            self.e.request_stop()

    async def handle_resume(self, slept_s: float, source: str) -> None:
        """Hold scheduled dispatch, wait for the network (up to the window), release, then
        catch up once. Idempotent for the several signals one wake can produce."""
        e = self.e
        mono = e.clock.monotonic()
        if self._resuming or mono - self._last_resume < RESUME_DEBOUNCE_S:
            return
        self._resuming, self._last_resume = True, mono
        self.resumes += 1
        # The hold stays until the overdue bookmarks have been re-timed: releasing first would
        # let the scheduler start every one of them at once, as plain scheduled checks.
        e.scheduler.hold("resume")
        try:
            log.info("resume_detected", slept_s=round(slept_s), source=source)
            e.events.publish("engine_state", {"reason": "resumed", "slept_s": round(slept_s)})
            e.actions.kick()
            self.poll_battery()  # the charger may have been plugged in (or out) meanwhile
            online = e.online
            if e.connectivity is not None:
                online = await e.connectivity.wait_for_network(e.settings.resume_probe_window_s)
            if online:
                await e.catch_up("resume")
        finally:
            e.scheduler.release("resume")
            self._resuming = False

    # -- battery ------------------------------------------------------------------------

    async def _battery_loop(self) -> None:
        while True:
            await self.e.clock.sleep(self.e.settings.battery_poll_s)
            self.poll_battery()

    def poll_battery(self) -> None:
        e = self.e
        try:
            on_battery = self.backend.on_battery()
            saver = self.backend.battery_saver() and e.settings.pause_on_battery_saver
        except Exception:
            log.warning("battery_read_failed", exc_info=True)
            return
        e.set_on_battery(on_battery)
        if saver:
            if "saver" not in e.scheduler.holds:
                log.info("battery_saver_pause")
            e.scheduler.hold("saver")
        else:
            e.scheduler.release("saver")

    def apply_keep_awake(self) -> None:
        """ "Keep awake while AutoWatch runs": on only while the option is set and not paused."""
        want = self.e.settings.keep_awake and not self.e.scheduler.paused
        with contextlib.suppress(Exception):
            self.backend.keep_awake(want)
