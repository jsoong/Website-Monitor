"""Stretching an over-limit host's intervals (spec: Scheduler -> Capacity).

    "With 2 s spacing, one host serves at most 30 checks a minute. When a host's demand exceeds
    its limit, the editor warns and the scheduler stretches that host's intervals evenly instead of
    letting the queue grow."

The demand is measured the way the Capacity page measures it (``capacity.py``): what the
schedules ask for, per host. The ceiling is ``60 / minimum spacing`` checks a minute from the
host's own limits, overrides included. A host asking for 120 a minute against a ceiling of 30
gets a factor of 4: each of its bookmarks waits four times as long, so they all slow down
**evenly** and relative priorities survive.

The factor is applied when the next due time is computed (``schedule.next_due(stretch=...)``),
never to the stored adaptive interval, so it cannot feed back into the demand it came from. It
is re-measured every ``host_load_refresh_s`` and whenever the settings change.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pagewatch.engine import capacity
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.hostload")


class HostLoad:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._factors: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()

    def factor(self, host: str) -> float:
        """How many times longer than asked this host's checks wait (1.0 = not stretched)."""
        if not self.e.settings.stretch_hosts:
            return 1.0
        return self._factors.get(host, 1.0)

    @property
    def stretched(self) -> dict[str, float]:
        return dict(self._factors) if self.e.settings.stretch_hosts else {}

    async def refresh(self) -> dict[str, float]:
        """Measure every host's demand now."""
        async with self._refresh_lock:
            rows = await self.e.db.read(repo.demand_rows)
            _, hosts = await asyncio.to_thread(capacity.tally, self.e, rows)
            factors: dict[str, float] = {}
            for host, (_count, per_s) in hosts.items():
                _, gap = self.e.host_gate.limits(host)
                if gap <= 0.0:
                    continue  # no spacing, no ceiling
                ratio = (per_s * 60.0) / (60.0 / gap)
                if ratio > 1.0:
                    factors[host] = ratio
            if factors != self._factors:
                log.info("hosts_stretched", hosts=len(factors))
            self._factors = factors
            return factors

    def refresh_soon(self) -> None:
        """Re-measure in the background (the settings or a bookmark changed)."""
        self.e.spawn(self.refresh(), "host-load")

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="host-load")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            await self.e.clock.sleep(max(1.0, self.e.settings.host_load_refresh_s))
            try:
                await self.refresh()
            except Exception:
                log.exception("host_load_failed")
