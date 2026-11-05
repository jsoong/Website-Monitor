"""Demand against capacity (spec: *Scheduler → Capacity at 1,000 to 10,000 bookmarks*).

"Settings shows current demand per pool and per host" and "browser demand against capacity,
measured from the rolling average check time, and warns above 80%". Read-only; nothing here
changes how checks are dispatched (see ``engine/stretch.py`` for what does).

* **Demand** of a bookmark is the checks per second its schedule asks for: ``1 / interval`` (the
  adaptive interval it currently has, when adaptive), ``n x days / week`` for fixed times, nothing
  for manual ones. Windows and day limits are ignored, so demand is an upper bound.
* **Pool capacity** is ``pool size / mean check time`` over the last day's timed checks. With
  fewer than ``MIN_SAMPLES`` checks there is no honest figure, so capacity stays ``None`` instead of
  a guess.
* **Host ceiling** is ``60 / minimum spacing`` checks a minute (2 s spacing: 30 a minute), from the
  host's own limits, overrides included.

This module only measures. The scheduler's response to an over-limit host is ``engine/stretch.py``,
which reuses ``tally`` and ``host_ceiling_per_min``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pagewatch.engine.clock import iso
from pagewatch.engine.config import effective_section
from pagewatch.engine.hostgate import host_of
from pagewatch.engine.scheduler import uses_browser
from pagewatch.engine.store import repo
from pagewatch.models import CapacityOut, HostDemand, PoolDemand, ScheduleMode

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

WARN_UTILISATION = 0.8
ROUNDING = 1e-6  # demand is a sum of many 1/interval terms: "exactly at the limit" is not "over"
MIN_SAMPLES = 5
WINDOW = timedelta(hours=24)
HOSTS_SHOWN = 50
_WEEK_S = 7 * 86400.0


def checks_per_second(sched: dict[str, Any], current_interval_s: int | None) -> float:
    """What one bookmark's schedule asks for, in checks per second."""
    try:
        mode = ScheduleMode(sched.get("mode", "interval"))
    except ValueError:
        return 0.0
    if mode is ScheduleMode.MANUAL:
        return 0.0
    if mode is ScheduleMode.TIMES:
        times = sched.get("times") or []
        days = sched.get("days") or []
        return len(times) * (len(days) or 7) / _WEEK_S
    if mode is ScheduleMode.ADAPTIVE:
        interval = current_interval_s or (sched.get("adaptive") or {}).get("min_s") or 900
    else:
        interval = sched.get("interval_s") or 3600
    return 1.0 / max(float(interval), 1.0)


def tally(
    engine: Engine, rows: list[sqlite3.Row]
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """pool -> [bookmarks, checks/s]; host -> [bookmarks, checks/s]. Runs off the event loop:
    it resolves every bookmark's effective schedule."""
    pools: dict[str, list[float]] = {"static": [0.0, 0.0], "browser": [0.0, 0.0]}
    hosts: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        sched = effective_section("schedule", row, engine.folders, engine.settings)
        rate = checks_per_second(sched, row["current_interval_s"])
        if rate <= 0.0:
            continue
        browser = engine.scheduler.uses_browser_pool(row["id"])
        if browser is None:
            browser = uses_browser(row["check_method"])
        for tally in (pools["browser" if browser else "static"], hosts[host_of(row["url"])]):
            tally[0] += 1
            tally[1] += rate
    return pools, hosts


def exceeds(demand_per_min: float, ceiling_per_min: float) -> bool:
    """Whether a host is asked for more than it can serve (beyond rounding noise)."""
    return demand_per_min > ceiling_per_min * (1.0 + ROUNDING)


def host_ceiling_per_min(engine: Engine, host: str) -> float | None:
    """What a host can serve: ``60 / minimum spacing`` checks a minute (None: no spacing, so no
    ceiling). Overrides count."""
    _, gap = engine.host_gate.limits(host)
    return 60.0 / gap if gap > 0.0 else None


async def compute(engine: Engine, *, hosts_shown: int = HOSTS_SHOWN) -> CapacityOut:
    now = engine.clock.now()
    since = iso(now - WINDOW)

    def read(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict[str, tuple[int, float]]]:
        return repo.demand_rows(conn), repo.check_time_stats(conn, since)

    rows, stats = await engine.db.read(read)
    pools_raw, hosts_raw = await asyncio.to_thread(tally, engine, rows)
    s = engine.settings

    pools: list[PoolDemand] = []
    for name, size in (("static", s.static_pool), ("browser", s.browser_pool)):
        count, per_s = pools_raw[name]
        samples, mean = stats.get(name, (0, 0.0))
        capacity = size / mean if samples >= MIN_SAMPLES and mean > 0.0 else None
        util = per_s / capacity if capacity else None
        pools.append(
            PoolDemand(
                name=name,
                bookmarks=int(count),
                demand_per_s=per_s,
                size=size,
                mean_check_s=mean if samples >= MIN_SAMPLES else None,
                samples=samples,
                capacity_per_s=capacity,
                utilisation=util,
                warn=util is not None and util > WARN_UTILISATION,
            )
        )

    hosts: list[HostDemand] = []
    for host, (count, per_s) in hosts_raw.items():
        ceiling = host_ceiling_per_min(engine, host)
        per_min = per_s * 60.0
        util = per_min / ceiling if ceiling else None
        hosts.append(
            HostDemand(
                host=host,
                bookmarks=int(count),
                demand_per_min=per_min,
                ceiling_per_min=ceiling,
                utilisation=util,
                over_limit=ceiling is not None and exceeds(per_min, ceiling),
                stretch=engine.host_stretch.factor(host),
            )
        )
    hosts.sort(key=lambda h: (h.utilisation or 0.0, h.demand_per_min, h.host), reverse=True)
    return CapacityOut(
        pools=pools,
        hosts=hosts[: max(1, hosts_shown)],
        hosts_total=len(hosts),
        hosts_over_limit=sum(h.over_limit for h in hosts),
        warn_utilisation=WARN_UTILISATION,
        generated_at=iso(now),
    )
