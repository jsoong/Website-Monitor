"""The resource guard: memory, samples, and the event-loop watchdog."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import psutil
import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import EXIT_RESTART, Engine
from pagewatch.engine.guard import LoopWatchdog, ResourceGuard, tree_rss_mb


class Probe:
    def __init__(self, mb: float) -> None:
        self.mb = mb

    def __call__(self) -> float:
        return self.mb


def guard_for(engine: Engine, probe: Probe) -> tuple[ResourceGuard, list[str]]:
    recycled: list[str] = []

    async def recycle(reason: str) -> bool:
        recycled.append(reason)
        return True

    engine.browser.recycle = recycle  # type: ignore[method-assign]
    return ResourceGuard(engine, rss_probe=probe, watchdog=False), recycled


# -- memory -----------------------------------------------------------------------------------


def test_rss_counts_the_children_not_just_the_process() -> None:
    before = tree_rss_mb()
    child = subprocess.Popen(
        [sys.executable, "-c", "x = bytearray(80 * 1024 * 1024)\nimport time\ntime.sleep(30)"]
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and tree_rss_mb() - before < 60:
            time.sleep(0.1)
        own = psutil.Process().memory_info().rss / 1024 / 1024
        assert tree_rss_mb() - own > 60  # the child's memory is in the total
    finally:
        child.kill()
        child.wait()


async def test_under_the_limit_nothing_happens(engine: Engine) -> None:
    guard, recycled = guard_for(engine, Probe(300))
    assert await guard.check_rss() is False and not recycled and engine.exit_code == 0
    assert guard.last_rss_mb == 300


async def test_over_the_limit_the_browser_is_recycled_first_then_the_engine_restarts(
    engine: Engine, clock: FakeClock
) -> None:
    await engine.update_settings({"rss_limit_mb": 1500, "rss_grace_s": 60})
    probe = Probe(1700)
    guard, recycled = guard_for(engine, probe)
    assert await guard.check_rss() is False and recycled == ["rss"]  # step one
    clock.advance(30)
    assert await guard.check_rss() is False and recycled == ["rss"]  # still within the grace
    assert not engine._stopped.is_set()
    clock.advance(31)
    assert await guard.check_rss() is True  # still over: give up
    assert engine.exit_code == EXIT_RESTART and engine._stopped.is_set()
    assert recycled == ["rss"]  # the browser was recycled once, not on every look


async def test_recycling_the_browser_is_enough_when_memory_comes_back_down(
    engine: Engine, clock: FakeClock
) -> None:
    await engine.update_settings({"rss_limit_mb": 1500, "rss_grace_s": 60})
    probe = Probe(1700)
    guard, recycled = guard_for(engine, probe)
    await guard.check_rss()
    probe.mb = 900  # the browser took its memory with it
    clock.advance(120)
    assert await guard.check_rss() is False and engine.exit_code == 0
    probe.mb = 1700  # and later it grows again: the cycle starts over, with a new recycle
    await guard.check_rss()
    assert recycled == ["rss", "rss"]


async def test_the_guard_loop_checks_on_the_engine_clock(engine: Engine, clock: FakeClock) -> None:
    await engine.update_settings({"rss_check_s": 30, "rss_grace_s": 0, "rss_limit_mb": 1000})
    guard, recycled = guard_for(engine, Probe(5000))
    await guard.start()
    try:
        await clock.settle()
        clock.advance(31)
        await clock.settle()
        await asyncio.sleep(0.05)
        assert recycled == ["rss"]
        clock.advance(31)
        await clock.settle()
        await asyncio.sleep(0.05)
        assert engine.exit_code == EXIT_RESTART
    finally:
        await guard.stop()


async def test_the_browser_recycle_is_a_no_op_when_none_is_running(engine: Engine) -> None:
    assert await engine.browser.recycle("rss") is False


# -- samples ----------------------------------------------------------------------------------


async def test_a_sample_goes_into_the_metric_table_and_health(engine: Engine) -> None:
    guard, _ = guard_for(engine, Probe(321.5))
    values = await guard.sample()
    assert values["rss_mb"] == 321.5 and set(values) == {
        "rss_mb",
        "rss_engine_mb",
        "cpu_pct",
        "queue_length",
        "in_flight",
    }
    rows = await engine.db.read(
        lambda c: {r["name"]: r["value"] for r in c.execute("SELECT * FROM metric")}
    )
    assert rows["rss_mb"] == 321.5 and "queue_length" in rows
    assert guard.last_rss_mb == 321.5


async def test_samples_are_taken_at_the_configured_interval(
    engine: Engine, clock: FakeClock
) -> None:
    await engine.update_settings({"metric_interval_s": 3600})
    guard, _ = guard_for(engine, Probe(100))
    await guard.start()
    try:
        await clock.settle()
        await asyncio.sleep(0.05)
        for _ in range(3):
            clock.advance(3600)
            await clock.settle()
            await asyncio.sleep(0.05)
        n = await engine.db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM metric WHERE name='rss_mb'").fetchone()[0]
        )
        assert n == 4  # at start, then once an hour
    finally:
        await guard.stop()


# -- the watchdog -----------------------------------------------------------------------------


def blocks_the_loop_for(seconds: float) -> None:
    time.sleep(seconds)


async def test_a_blocked_loop_is_reported_with_the_stack_of_what_blocked_it() -> None:
    reports: list[tuple[float, str]] = []
    dog = LoopWatchdog(
        lambda: 0.3, beat_s=0.05, poll_s=0.05, on_stall=lambda lag, s: reports.append((lag, s))
    )
    await dog.start()
    try:
        await asyncio.sleep(0.2)
        assert not reports  # a healthy loop is silent
        blocks_the_loop_for(0.9)  # the loop is stuck, as a runaway regex would leave it
        await asyncio.sleep(0.3)
    finally:
        await dog.stop()
    assert len(reports) == 1, "one report per stall, not one per poll"
    lag, stack = reports[0]
    assert lag > 0.3 and "blocks_the_loop_for" in stack and "time.sleep" in stack
    assert dog.stalls == 1


async def test_a_second_stall_after_recovery_is_reported_again() -> None:
    reports: list[float] = []
    dog = LoopWatchdog(
        lambda: 0.2, beat_s=0.05, poll_s=0.05, on_stall=lambda lag, s: reports.append(lag)
    )
    await dog.start()
    try:
        await asyncio.sleep(0.1)
        blocks_the_loop_for(0.5)
        await asyncio.sleep(0.3)  # recovered
        blocks_the_loop_for(0.5)
        await asyncio.sleep(0.3)
    finally:
        await dog.stop()
    assert len(reports) == 2


@pytest.mark.parametrize("busy", [0.0, 0.05])
async def test_short_hiccups_are_not_stalls(busy: float) -> None:
    reports: list[float] = []
    dog = LoopWatchdog(
        lambda: 0.5, beat_s=0.05, poll_s=0.05, on_stall=lambda lag, s: reports.append(lag)
    )
    await dog.start()
    try:
        for _ in range(5):
            blocks_the_loop_for(busy)
            await asyncio.sleep(0.05)
    finally:
        await dog.stop()
    assert not reports
