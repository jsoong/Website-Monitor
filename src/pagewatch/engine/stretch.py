"""Over-limit hosts (spec: *Scheduler → Capacity at 1,000 to 10,000 bookmarks*): "When a host's
demand exceeds its limit, ... the scheduler stretches that host's intervals evenly instead of
letting the queue grow."

* A host's **demand** is what its bookmarks' schedules ask for and its **ceiling** is what its
  minimum spacing allows; both come from ``engine/capacity.py``, so what Settings shows and what
  the scheduler does cannot disagree.
* When demand is ``f`` times the ceiling (``f > 1``), every interval of that host is made ``f``
  times longer: the same factor for each of its bookmarks ("evenly"), so what is asked for ends up
  equal to what the host can serve. Below the ceiling the factor is 1 and nothing changes.
* Only the **gap before the next check** is stretched (``schedule.next_due(stretch=...)``). The
  stored adaptive interval is not, and demand is computed from the unstretched schedules, so the
  figure does not feed back into itself and the factor falls back as soon as demand does.
* Not affected: manual "Check now", fixed-time schedules (``times``: clock times, not gaps) and
  the first check of a new bookmark.
* Like the capacity figures, demand ignores windows, days and the battery slow-down, so it is an
  upper bound and the stretch errs on the long side.

The factors are refreshed at start-up, when settings change and, after a check, at most once a
minute; a bookmark added in between is accounted for within that minute.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pagewatch.engine import capacity
from pagewatch.engine.hostgate import host_of
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.stretch")

REFRESH_S = 60.0


class HostStretch:
    def __init__(self, engine: Engine) -> None:
        self._e = engine
        self._factors: dict[str, float] = {}
        self._last: float | None = None  # engine monotonic time of the last refresh
        self._running = False
        self._queued = False  # a refresh has been spawned and has not started yet

    @property
    def factors(self) -> dict[str, float]:
        """Host -> factor, for hosts that are over their limit only."""
        return dict(self._factors)

    def factor(self, host: str) -> float:
        return self._factors.get(host, 1.0)

    def for_url(self, url: str) -> float:
        return self.factor(host_of(url))

    async def refresh(self) -> None:
        """Measure every host again. Never raises: a failed measurement keeps the old factors."""
        if self._running:
            return
        self._running = True
        try:
            e = self._e
            rows = await e.db.read(repo.demand_rows)
            _, hosts = await asyncio.to_thread(capacity.tally, e, rows)
            new: dict[str, float] = {}
            for host, (_count, per_s) in hosts.items():
                ceiling = capacity.host_ceiling_per_min(e, host)
                if ceiling and capacity.exceeds(per_s * 60.0, ceiling):
                    new[host] = per_s * 60.0 / ceiling
            self._report(new)
            self._factors = new
            self._last = e.clock.monotonic()
        except Exception as exc:  # the scheduler must not depend on this succeeding
            log.warning("host_stretch_failed", error=str(exc)[:200])
        finally:
            self._running = False

    def _report(self, new: dict[str, float]) -> None:
        """Say so when a host starts or stops being slowed down (not on every small change)."""
        for host, f in new.items():
            before = self._factors.get(host)
            if before is None or abs(f - before) / before > 0.25:
                log.info("host_stretched", host=host, factor=round(f, 2))
        for host in self._factors.keys() - new.keys():
            log.info("host_unstretched", host=host)

    def refresh_soon(self, *, force: bool = False) -> None:
        """Refresh in the background unless one is running or queued, or the last one is recent."""
        if self._running or self._queued:
            return
        recent = self._last is not None and self._e.clock.monotonic() - self._last < REFRESH_S
        if recent and not force:
            return
        self._queued = True
        self._e.spawn(self._run(), "host-stretch")

    async def _run(self) -> None:
        self._queued = False
        await self.refresh()
