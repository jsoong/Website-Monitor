"""Resource guard and observability (spec: Continuous operation -> Resource limits, Observability).

* **Memory.** The engine's RSS (its own process plus its workers and browser) is checked every
  ``rss_check_s``. Over ``rss_limit_mb`` the browser is recycled; if the number is still over the
  limit ``rss_grace_s`` later, the engine exits with code 3 so its supervisor starts a fresh one.
  A guard that only looked at the Python process could not tell whether recycling the browser
  helped, so the children are counted.
* **Samples.** Every ``metric_interval_s`` (hourly) RSS, CPU, queue length and in-flight checks go
  into the ``metric`` table and ``/health`` reports the latest.
* **Event-loop stalls.** A watchdog *thread* notices when the loop has not run for
  ``loop_lag_limit_s`` and logs the loop thread's stack while it is still stuck, which is the only
  moment the culprit can be seen. (The loop cannot report on itself while it is blocked.)
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

import psutil

from pagewatch.engine.clock import iso
from pagewatch.engine.logs import get_logger

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.guard")

MIB = 1024 * 1024


def tree_rss_mb(proc: psutil.Process | None = None) -> float:
    """RSS of this process plus every descendant (workers, browser), in MiB."""
    proc = proc or psutil.Process()
    total = proc.memory_info().rss
    for child in proc.children(recursive=True):
        with contextlib.suppress(psutil.Error):
            total += child.memory_info().rss
    return total / MIB


class LoopWatchdog:
    """Logs a stack dump when the event loop has been blocked for more than ``limit_s``."""

    def __init__(
        self,
        limit_s: Callable[[], float],
        *,
        beat_s: float = 1.0,
        poll_s: float = 1.0,
        on_stall: Callable[[float, str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit_s
        self._beat_s = beat_s
        self._poll_s = poll_s
        self._on_stall = on_stall
        self._mono = monotonic
        self._last_beat = monotonic()
        self._loop_thread: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._beater: asyncio.Task[None] | None = None
        self._reported = False
        self.stalls = 0

    async def start(self) -> None:
        self._loop_thread = threading.get_ident()
        self._last_beat = self._mono()
        self._beater = asyncio.create_task(self._beat(), name="loop-heartbeat")
        self._thread = threading.Thread(target=self._watch, name="pw-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._beater is not None:
            self._beater.cancel()
            await asyncio.gather(self._beater, return_exceptions=True)
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _beat(self) -> None:
        while True:
            self._last_beat = self._mono()
            await asyncio.sleep(self._beat_s)  # real time: lag is about the real loop

    def _watch(self) -> None:
        while not self._stop.wait(self._poll_s):
            lag = self._mono() - self._last_beat - self._beat_s
            if lag > self._limit():
                if not self._reported:
                    self._reported = True
                    self.stalls += 1
                    self._report(lag)
            elif self._reported:
                self._reported = False
                log.info("event_loop_recovered")

    def _report(self, lag: float) -> None:
        frame = sys._current_frames().get(self._loop_thread or 0)
        stack = "".join(traceback.format_stack(frame)) if frame is not None else "(no stack)"
        log.error("event_loop_blocked", lag_s=round(lag, 1), stack=stack)
        if self._on_stall is not None:
            with contextlib.suppress(Exception):
                self._on_stall(lag, stack)


class ResourceGuard:
    def __init__(
        self,
        engine: Engine,
        *,
        rss_probe: Callable[[], float] = tree_rss_mb,
        watchdog: bool = True,
    ) -> None:
        self.e = engine
        self._rss = rss_probe
        self._proc = psutil.Process()
        self._tasks: list[asyncio.Task[None]] = []
        self._recycled_at: float | None = None
        self.watchdog = LoopWatchdog(lambda: self.e.settings.loop_lag_limit_s) if watchdog else None
        self.last_rss_mb: float | None = None
        self.last_cpu_percent: float | None = None
        self.recycles = 0

    async def start(self) -> None:
        self._proc.cpu_percent(None)  # the first call only primes the counter
        self._tasks = [
            asyncio.create_task(self._rss_loop(), name="rss-guard"),
            asyncio.create_task(self._sample_loop(), name="metrics"),
        ]
        if self.watchdog is not None:
            await self.watchdog.start()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.watchdog is not None:
            await self.watchdog.stop()

    # -- memory -------------------------------------------------------------------------

    async def _rss_loop(self) -> None:
        while True:
            await self.e.clock.sleep(self.e.settings.rss_check_s)
            if await self.check_rss():
                return

    async def check_rss(self) -> bool:
        """One look at memory. Returns True when the engine has asked to be restarted."""
        e = self.e
        s = e.settings
        rss = await asyncio.to_thread(self._rss)
        self.last_rss_mb = rss
        if rss <= s.rss_limit_mb:
            self._recycled_at = None
            return False
        now = e.clock.monotonic()
        if self._recycled_at is None:
            log.warning("rss_over_limit", rss_mb=round(rss), limit_mb=s.rss_limit_mb,
                        action="recycle_browser")  # fmt: skip
            self.recycles += 1
            self._recycled_at = now
            await e.browser.recycle("rss")
            return False
        if now - self._recycled_at >= s.rss_grace_s:
            log.error("rss_still_over_limit", rss_mb=round(rss), limit_mb=s.rss_limit_mb,
                      action="restart")  # fmt: skip
            from pagewatch.engine.core import EXIT_RESTART  # (core imports this module)

            e.events.publish("problem", {"kind": "engine_restart", "message": "memory limit"})
            e.request_stop(EXIT_RESTART)
            return True
        return False

    # -- samples ------------------------------------------------------------------------

    async def _sample_loop(self) -> None:
        while True:
            await self.sample()
            await self.e.clock.sleep(self.e.settings.metric_interval_s)

    async def sample(self) -> dict[str, float]:
        e = self.e
        rss_total = await asyncio.to_thread(self._rss)
        cpu = self._proc.cpu_percent(None) / max(1, psutil.cpu_count() or 1)
        values = {
            "rss_mb": round(rss_total, 1),
            "rss_engine_mb": round(self._proc.memory_info().rss / MIB, 1),
            "cpu_pct": round(cpu, 2),  # this process, as a share of the whole machine
            "queue_length": float(e.scheduler.queue_length),
            "in_flight": float(e.scheduler.in_flight),
        }
        self.last_rss_mb, self.last_cpu_percent = rss_total, cpu
        ts = iso(e.clock.now())
        await e.db.write(
            lambda c: c.executemany(
                "INSERT INTO metric(ts, name, value) VALUES(?,?,?)",
                [(ts, name, v) for name, v in values.items()],
            )
        )
        return values
