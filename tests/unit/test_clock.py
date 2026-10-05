import asyncio
from datetime import UTC, datetime, timedelta

from pagewatch.engine.clock import FakeClock, SystemClock, iso, parse_iso


def test_iso_roundtrip_and_ordering() -> None:
    a = datetime(2026, 3, 8, 7, 5, 1, 250000, tzinfo=UTC)
    b = a + timedelta(microseconds=1)
    assert parse_iso(iso(a)) == a
    assert iso(a) < iso(b)
    assert iso(datetime(2026, 12, 1, tzinfo=UTC)) > iso(datetime(2026, 2, 1, tzinfo=UTC))


async def test_fake_clock_sleepers_wake_in_order() -> None:
    clock = FakeClock()
    woke: list[str] = []

    async def sleeper(name: str, secs: float) -> None:
        await clock.sleep(secs)
        woke.append(name)

    tasks = [asyncio.create_task(sleeper("b", 20)), asyncio.create_task(sleeper("a", 10))]
    await clock.settle()
    assert woke == [] and clock.pending_sleepers == 2
    await clock.run_for(15)
    assert woke == ["a"]
    await clock.run_for(10)
    assert woke == ["a", "b"]
    await asyncio.gather(*tasks)


async def test_jump_wall_moves_only_wall_time() -> None:
    clock = FakeClock()
    w0, m0 = clock.now(), clock.monotonic()
    clock.jump_wall(3 * 3600)
    assert clock.now() - w0 == timedelta(hours=3)
    assert clock.monotonic() == m0


async def test_run_for_lets_chained_timers_fire() -> None:
    clock = FakeClock()
    ticks = 0

    async def loop() -> None:
        nonlocal ticks
        while True:
            await clock.sleep(60)
            ticks += 1

    task = asyncio.create_task(loop())
    await clock.run_for(3600)
    assert ticks == 60
    task.cancel()


async def test_system_clock_is_utc_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
