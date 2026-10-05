"""Schedule math (AutoWatch). Pure functions: no I/O, no clock reads, injectable RNG.

All instants are timezone-aware UTC. ``days`` and ``window`` and ``times`` are evaluated on
the *local* wall clock through a ``Zone`` so DST shifts are handled; an IANA zone (via
``zoneinfo``) when one is configured, otherwise the operating system's local time.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from pagewatch.models import DAYS, MIN_INTERVAL_S, OnBattery, ScheduleConfig, ScheduleMode

SLOW_FACTOR = 4


class Zone(Protocol):
    def localize(self, naive_local: datetime) -> datetime:
        """Local wall time -> aware UTC (DST gaps move forward, ambiguous times take the first)."""

    def to_local(self, aware: datetime) -> datetime:
        """Aware instant -> naive local wall time."""


class SystemZone:
    """The operating system's local time zone (DST-aware through the C library)."""

    def localize(self, naive_local: datetime) -> datetime:
        return naive_local.astimezone(UTC)

    def to_local(self, aware: datetime) -> datetime:
        return aware.astimezone().replace(tzinfo=None)


class IanaZone:
    def __init__(self, name: str) -> None:
        self._zone = ZoneInfo(name)

    def localize(self, naive_local: datetime) -> datetime:
        return naive_local.replace(tzinfo=self._zone, fold=0).astimezone(UTC)

    def to_local(self, aware: datetime) -> datetime:
        return aware.astimezone(self._zone).replace(tzinfo=None)


def make_zone(name: str | None) -> Zone:
    return IanaZone(name) if name else SystemZone()


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def _day_ok(cfg: ScheduleConfig, d: date) -> bool:
    return not cfg.days or DAYS[d.weekday()] in cfg.days


def _in_window(cfg: ScheduleConfig, t: time) -> bool:
    if cfg.window is None:
        return True
    start, end = _hm(cfg.window.start), _hm(cfg.window.end)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # wraps midnight


def apply_limits(cfg: ScheduleConfig, due: datetime, zone: Zone) -> datetime:
    """Move ``due`` to the next allowed start if it falls on a disallowed day or outside the
    window. Returns ``due`` unchanged when it is allowed."""
    if not cfg.days and cfg.window is None:
        return due
    local = zone.to_local(due)
    d = local.date()
    if _day_ok(cfg, d) and _in_window(cfg, local.time()):
        return due
    start = _hm(cfg.window.start) if cfg.window else time(0, 0)
    # Outside the window on an allowed day and before it opens (which is always the case for a
    # window that wraps midnight): it opens later today. Otherwise: the next allowed day.
    if _day_ok(cfg, d) and cfg.window and local.time() < start:
        return zone.localize(datetime.combine(d, start))
    for offset in range(1, 9):
        nd = d + timedelta(days=offset)
        if _day_ok(cfg, nd):
            return zone.localize(datetime.combine(nd, start))
    return due  # unreachable: days is non-empty and covers every weekday within 7 days


def next_time_of_day(cfg: ScheduleConfig, now: datetime, zone: Zone) -> datetime | None:
    """The next fixed local clock time strictly after ``now`` on an allowed day."""
    local_now = zone.to_local(now)
    for offset in range(0, 9):
        d = local_now.date() + timedelta(days=offset)
        if not _day_ok(cfg, d):
            continue
        for t in cfg.times:
            cand = zone.localize(datetime.combine(d, _hm(t)))
            if cand > now:
                return cand
    return None


def effective_interval_s(cfg: ScheduleConfig, current: int | None, *, on_battery: bool) -> int:
    base = cfg.interval_s
    if cfg.mode is ScheduleMode.ADAPTIVE:
        base = current if current is not None else cfg.adaptive.min_s
    if on_battery and cfg.on_battery is OnBattery.SLOW:
        base *= SLOW_FACTOR
    return base


def adaptive_next_interval(
    cfg: ScheduleConfig, current: int | None, *, changed: bool | None
) -> int:
    """Unchanged: interval x factor up to max. Changed (a new filtered hash against latest,
    whether or not it alerted): reset to min. ``None`` (an error): keep the interval."""
    a = cfg.adaptive
    if current is None or changed:
        return a.min_s
    if changed is False:
        return min(a.max_s, max(a.min_s, int(current * a.factor)))
    return current


def jittered(seconds: float, pct: int, rng: random.Random) -> float:
    if pct <= 0:
        return seconds
    spread = rng.uniform(-pct, pct) / 100.0
    return max(float(MIN_INTERVAL_S), seconds * (1.0 + spread))


def next_due(
    cfg: ScheduleConfig,
    now: datetime,
    *,
    current_interval_s: int | None = None,
    changed: bool | None = None,
    on_battery: bool = False,
    zone: Zone | None = None,
    rng: random.Random | None = None,
) -> tuple[datetime | None, int | None]:
    """When the next check is due and the adaptive state to store.

    ``changed`` is only meaningful for adaptive schedules: ``True`` after a real change,
    ``False`` after an unchanged check, ``None`` after an error.
    """
    zone = zone or SystemZone()
    rng = rng or random.Random()
    if cfg.mode is ScheduleMode.MANUAL:
        return None, current_interval_s
    state = current_interval_s
    if cfg.mode is ScheduleMode.TIMES:
        due = next_time_of_day(cfg, now, zone)
        return (apply_limits(cfg, due, zone) if due else None), state
    if cfg.mode is ScheduleMode.ADAPTIVE:
        state = adaptive_next_interval(cfg, current_interval_s, changed=changed)
    interval = effective_interval_s(cfg, state, on_battery=on_battery)
    due = now + timedelta(seconds=jittered(interval, cfg.jitter_pct, rng))
    return apply_limits(cfg, due, zone), state
