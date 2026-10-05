"""Injectable time source.

Nothing in the engine calls ``datetime.now()``, ``time.monotonic()`` or ``asyncio.sleep()``
directly: everything goes through a ``Clock`` so tests can drive hours of simulated time
(and simulated sleep/resume) deterministically.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def iso(dt: datetime) -> str:
    """Fixed-width UTC timestamp; lexicographic order equals chronological order."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime(ISO_FMT)


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, ISO_FMT).replace(tzinfo=UTC)


class Clock(Protocol):
    def now(self) -> datetime:
        """Wall-clock time, timezone-aware UTC."""

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards."""

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class FakeClock:
    """Deterministic clock. Time moves only when a test says so."""

    def __init__(self, start: datetime | None = None) -> None:
        self._wall = start or datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)
        self._mono = 1000.0
        self._seq = 0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (self._mono + seconds, self._seq, fut))
        await fut

    # -- test controls ------------------------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Move both clocks forward and release every sleeper whose deadline has passed."""
        self._wall += timedelta(seconds=seconds)
        self._mono += seconds
        self._release()

    def jump_wall(self, seconds: float) -> None:
        """Move only the wall clock: what a laptop suspend looks like to monotonic time."""
        self._wall += timedelta(seconds=seconds)

    def set_wall(self, when: datetime) -> None:
        self._wall = when

    def _release(self) -> None:
        while self._waiters and self._waiters[0][0] <= self._mono:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)

    @property
    def pending_sleepers(self) -> int:
        return sum(1 for _, _, f in self._waiters if not f.done())

    def next_deadline(self) -> float | None:
        while self._waiters and self._waiters[0][2].done():
            heapq.heappop(self._waiters)
        return self._waiters[0][0] if self._waiters else None

    async def settle(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def run_for(self, seconds: float, *, max_step: float = 3600.0) -> None:
        """Advance ``seconds`` stepping through every timer in order, letting tasks react
        between steps, so sleepers observe a consistent timeline."""
        end = self._mono + seconds
        await self.settle()
        while self._mono < end:
            nxt = self.next_deadline()
            target = end if nxt is None else min(max(nxt, self._mono), end)
            target = min(target, self._mono + max_step)
            self.advance(target - self._mono)
            await self.settle()
