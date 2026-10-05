import random
from datetime import UTC, datetime, timedelta

import pytest

from pagewatch.engine import schedule as sch
from pagewatch.models import ScheduleConfig

NY = sch.IanaZone("America/New_York")
RNG = random.Random(0)


def cfg(**kw: object) -> ScheduleConfig:
    return ScheduleConfig.model_validate({"jitter_pct": 0, **kw})


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def due(c: ScheduleConfig, now: datetime, **kw: object) -> datetime:
    d, _ = sch.next_due(c, now, zone=NY, rng=RNG, **kw)  # type: ignore[arg-type]
    assert d is not None
    return d


def test_interval_adds_the_interval() -> None:
    now = utc(2026, 5, 4, 12)
    assert due(cfg(interval_s=600), now) == now + timedelta(seconds=600)


def test_manual_mode_has_no_due_time() -> None:
    assert sch.next_due(cfg(mode="manual"), utc(2026, 1, 1), zone=NY)[0] is None


def test_jitter_stays_within_bounds_and_never_below_the_minimum() -> None:
    c = ScheduleConfig(interval_s=600, jitter_pct=10)
    now = utc(2026, 5, 4, 12)
    spreads = {sch.next_due(c, now, zone=NY, rng=random.Random(i))[0] for i in range(200)}
    secs = [(d - now).total_seconds() for d in spreads if d]
    assert min(secs) >= 540 and max(secs) <= 660 and len(spreads) > 50
    floor = ScheduleConfig(interval_s=60, jitter_pct=50)
    for i in range(100):
        d, _ = sch.next_due(floor, now, zone=NY, rng=random.Random(i))
        assert d is not None and (d - now).total_seconds() >= 60


def test_on_battery_slow_multiplies_and_normal_does_not() -> None:
    now = utc(2026, 5, 4, 12)
    assert due(cfg(interval_s=600, on_battery="slow"), now, on_battery=True) == now + timedelta(
        seconds=2400
    )
    assert due(cfg(interval_s=600, on_battery="slow"), now, on_battery=False) == now + timedelta(
        seconds=600
    )
    assert due(cfg(interval_s=600, on_battery="normal"), now, on_battery=True) == now + timedelta(
        seconds=600
    )


# -- adaptive ---------------------------------------------------------------------------


def test_adaptive_grows_when_unchanged_resets_on_change_and_holds_on_error() -> None:
    c = cfg(mode="adaptive", adaptive={"min_s": 900, "max_s": 4000, "factor": 2.0})
    now = utc(2026, 5, 4, 12)
    _, s = sch.next_due(c, now, current_interval_s=None, changed=True, zone=NY)
    assert s == 900  # first check / change -> min
    seq = []
    for _ in range(4):
        d, s = sch.next_due(c, now, current_interval_s=s, changed=False, zone=NY)
        seq.append(s)
        assert d == now + timedelta(seconds=s or 0)
    assert seq == [1800, 3600, 4000, 4000]  # capped at max
    assert sch.next_due(c, now, current_interval_s=3600, changed=None, zone=NY)[1] == 3600
    assert sch.next_due(c, now, current_interval_s=3600, changed=True, zone=NY)[1] == 900


# -- times, days, window, DST -----------------------------------------------------------


def test_times_picks_the_next_local_clock_time() -> None:
    c = cfg(mode="times", times=["09:00", "17:30"])
    # 2026-05-04 12:00 UTC = 08:00 EDT -> next is 09:00 EDT = 13:00 UTC
    assert due(c, utc(2026, 5, 4, 12)) == utc(2026, 5, 4, 13)
    # 10:00 EDT -> 17:30 EDT = 21:30 UTC
    assert due(c, utc(2026, 5, 4, 14)) == utc(2026, 5, 4, 21, 30)
    # after the last time -> tomorrow 09:00
    assert due(c, utc(2026, 5, 4, 22)) == utc(2026, 5, 5, 13)
    # strictly after: at exactly 09:00 the next one is 17:30
    assert due(c, utc(2026, 5, 4, 13)) == utc(2026, 5, 4, 21, 30)


def test_times_honours_allowed_days() -> None:
    c = cfg(mode="times", times=["09:00"], days=["mon", "wed"])
    # Tue 2026-05-05 -> Wed 2026-05-06 09:00 EDT
    assert due(c, utc(2026, 5, 5, 15)) == utc(2026, 5, 6, 13)
    # Wed after 09:00 -> next Monday (2026-05-11)
    assert due(c, utc(2026, 5, 6, 15)) == utc(2026, 5, 11, 13)


def test_times_across_spring_forward_dst() -> None:
    # 2026-03-08: clocks jump 02:00 EST -> 03:00 EDT in New York.
    c = cfg(mode="times", times=["09:00"])
    before = due(c, utc(2026, 3, 7, 15))  # Sat 10:00 EST -> next 09:00 is Sun, now EDT
    assert before == utc(2026, 3, 8, 13)  # 09:00 EDT, not 14:00 UTC (EST)
    assert due(c, utc(2026, 3, 8, 14)) == utc(2026, 3, 9, 13)


def test_a_time_inside_the_dst_gap_moves_forward_and_still_fires_once() -> None:
    c = cfg(mode="times", times=["02:30"])
    d = due(c, utc(2026, 3, 8, 3))  # Sat night local; 02:30 on 2026-03-08 does not exist
    assert d == utc(2026, 3, 8, 7, 30)  # PEP 495: 02:30 EST-equivalent = 03:30 EDT
    assert due(c, d) == utc(2026, 3, 9, 6, 30)  # next day it is a normal 02:30 EDT


def test_times_across_fall_back_dst() -> None:
    # 2026-11-01: 02:00 EDT -> 01:00 EST. 09:00 local exists once either side.
    c = cfg(mode="times", times=["09:00"])
    assert due(c, utc(2026, 10, 31, 14)) == utc(2026, 11, 1, 14)  # 09:00 EST = 14:00 UTC
    # an ambiguous 01:30 fires once, at its first occurrence (EDT)
    amb = cfg(mode="times", times=["01:30"])
    first = due(amb, utc(2026, 11, 1, 4))
    assert first == utc(2026, 11, 1, 5, 30)
    assert due(amb, first) == utc(2026, 11, 2, 6, 30)  # not again an hour later


def test_window_defers_to_the_next_start() -> None:
    c = cfg(interval_s=600, window={"start": "07:00", "end": "23:00"})
    # 02:00 EDT + 10 min is outside the window -> today's 07:00 EDT = 11:00 UTC
    assert due(c, utc(2026, 5, 4, 6)) == utc(2026, 5, 4, 11)
    # 22:55 EDT + 10 min = 23:05 -> tomorrow 07:00 EDT
    assert due(c, utc(2026, 5, 5, 2, 55)) == utc(2026, 5, 5, 11)
    # inside the window nothing moves
    now = utc(2026, 5, 4, 16)
    assert due(c, now) == now + timedelta(seconds=600)


def test_window_that_wraps_midnight() -> None:
    c = cfg(interval_s=600, window={"start": "22:00", "end": "06:00"})
    inside = utc(2026, 5, 5, 4)  # 00:00 EDT
    assert due(c, inside) == inside + timedelta(seconds=600)
    # 12:00 EDT is outside -> today 22:00 EDT = 02:00 UTC next day
    assert due(c, utc(2026, 5, 4, 16)) == utc(2026, 5, 5, 2)


def test_days_limit_applies_to_interval_mode_too() -> None:
    c = cfg(interval_s=3600, days=["sat"], window={"start": "10:00", "end": "12:00"})
    # Mon 12:00 EDT +1h is not Saturday -> Sat 2026-05-09 10:00 EDT
    assert due(c, utc(2026, 5, 4, 16)) == utc(2026, 5, 9, 14)


def test_times_mode_respects_window() -> None:
    c = cfg(mode="times", times=["05:00"], window={"start": "07:00", "end": "23:00"})
    assert due(c, utc(2026, 5, 4, 6)) == utc(2026, 5, 4, 11)  # 05:00 is outside -> 07:00


@pytest.mark.parametrize("name", [None, "America/New_York", "Europe/Berlin", "Asia/Kolkata"])
def test_localize_and_to_local_roundtrip(name: str | None) -> None:
    zone = sch.make_zone(name)
    now = utc(2026, 7, 1, 12)
    local = zone.to_local(now)
    assert local.tzinfo is None
    assert zone.localize(local) == now
