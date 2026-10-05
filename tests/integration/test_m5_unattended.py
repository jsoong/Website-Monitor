"""M5 acceptance, the sleep / network / battery half, through the whole engine under a FakeClock.

The engine here is built with ``enable_unattended`` (heartbeat, battery poll, connectivity) and
a scripted network: a probe that answers according to ``FakeNetwork.up`` and a fetcher that
fails with a connection error while it is down.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.instance import write_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.engine.power import NullPowerBackend
from pagewatch.models import FetchErrorKind, LockInfo
from tests.support.fakes import ScriptedFetcher, error, ok
from tests.support.m5 import SCHED, FakeNetwork, add, bookmark, runs
from tests.support.sim import advance, settle


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
        "worker_processes": 2, "toast_coalesce_s": 0.0, "timezone": "UTC",
    }  # fmt: skip


@pytest.fixture
def net() -> FakeNetwork:
    return FakeNetwork()


@pytest.fixture
def power() -> NullPowerBackend:
    return NullPowerBackend()


def make_engine(
    data_dir: DataDir, clock: FakeClock, overrides: dict[str, Any], toasts: LogToastBackend,
    power: NullPowerBackend, net: FakeNetwork,
) -> Engine:  # fmt: skip
    eng = Engine(
        data_dir, clock=clock, worker_mode="thread", settings_overrides=overrides,
        toast_backend=toasts, rng=random.Random(7), enable_unattended=True,
        power_backend=power, probe=net.probe,
    )  # fmt: skip
    eng.fetchers.replace("static", net.fetcher())  # before start: nothing real is ever fetched
    return eng


@pytest.fixture
async def engine(
    data_dir: DataDir, clock: FakeClock, settings_overrides: dict[str, Any],
    toasts: LogToastBackend, power: NullPowerBackend, net: FakeNetwork,
) -> AsyncIterator[Engine]:  # fmt: skip
    """Shadows the plain ``engine`` fixture so the ``api`` and ``client`` fixtures use it."""
    eng = make_engine(data_dir, clock, settings_overrides, toasts, power, net)
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


# -- M5: a simulated 3-hour sleep -----------------------------------------------------------


async def test_a_three_hour_sleep_yields_one_staggered_catchup_check_per_overdue_bookmark(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    ids = [await add(client, f"site{i}", schedule={"interval_s": 900, "jitter_pct": 0})
           for i in range(12)]  # fmt: skip
    await settle(engine, clock)
    for bid in ids:
        assert [r["outcome"] for r in await runs(client, bid)] == ["first"]

    clock.jump_wall(3 * 3600)  # the laptop sleeps: the wall clock moves, monotonic time does not
    await advance(engine, clock, 5, step=5)  # the heartbeat notices the drift
    assert engine.power is not None and engine.power.resumes == 1
    await advance(engine, clock, 20, step=1)  # the catch-up checks, one second apart
    await engine.drain_background()

    started: list[datetime] = []
    for bid in ids:
        rs = await runs(client, bid)
        # exactly one catch-up check, not one per missed 15-minute interval (12 of them)
        assert [r["trigger"] for r in rs] == ["schedule", "catchup"], rs
        assert rs[1]["outcome"] == "unchanged"
        started.append(at(rs[1]["started_at"]))
    assert len(set(started)) == 12  # staggered: no two at the same instant
    spread = (max(started) - min(started)).total_seconds()
    assert 10 <= spread <= 12  # over count x 1 s (12 bookmarks), not all at once

    # and then back to the normal rhythm: the next check is a full interval after its catch-up
    await advance(engine, clock, 600, step=30)
    for bid in ids:
        assert len(await runs(client, bid)) == 2


async def test_the_spread_is_capped_at_five_minutes_and_hotsites_go_first(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    normal = [await add(client, f"n{i}", schedule={"interval_s": 900, "jitter_pct": 0})
              for i in range(3)]  # fmt: skip
    hot = await add(client, "hot", priority=1, schedule={"interval_s": 900, "jitter_pct": 0})
    await settle(engine, clock)
    await engine.update_settings({"catchup_spread_s": 3})  # 4 overdue: min(3, 4 x 1 s) = 3 s
    clock.jump_wall(7200)
    await advance(engine, clock, 5, step=5)
    await advance(engine, clock, 6, step=1)
    await engine.drain_background()
    first = {b: at((await runs(client, b))[1]["started_at"]) for b in [hot, *normal]}
    assert first[hot] == min(first.values())  # hotsites first
    assert (max(first.values()) - min(first.values())).total_seconds() <= 3


# -- M5: 10 minutes offline -------------------------------------------------------------------


async def test_ten_minutes_offline_adds_zero_to_any_error_counter(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, net: FakeNetwork,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    ids = [await add(client, f"site{i}") for i in range(5)]
    await settle(engine, clock)

    net.up = False
    await advance(engine, clock, 600, step=10)

    assert engine.online is False
    assert (await client.get("/health")).json()["online"] is False
    for bid in ids:
        b = await bookmark(client, bid)
        assert b["consecutive_errors"] == 0 and b["status"] == "ok", b
        rs = await runs(client, bid)
        # the one check that found out is recorded as skipped (not an error); nothing after it
        assert [r["outcome"] for r in rs] == ["first", "skipped"], rs
        assert rs[1]["reason"] == "offline:connection"
    assert not toasts.shown  # nobody is told that "every site is failing"

    net.up = True  # the network is back: the recovery probe notices, then everything catches up
    await advance(engine, clock, 40, step=5)
    await engine.drain_background()
    await advance(engine, clock, 30, step=1)
    assert engine.online is True
    for bid in ids:
        rs = await runs(client, bid)
        assert [r["outcome"] for r in rs][-1] == "unchanged" and rs[-1]["trigger"] == "catchup"
        b = await bookmark(client, bid)
        assert b["consecutive_errors"] == 0 and b["status"] == "ok"
    assert not toasts.shown


async def test_a_site_that_is_down_while_the_network_is_up_still_counts_normally(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, net: FakeNetwork,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    """The probe is what tells "we are offline" from "that site is down": here the network
    answers the probe but the page fetch fails."""
    bid = await add(client, "flaky")
    await settle(engine, clock)
    engine.fetchers.replace(
        "static",
        ScriptedFetcher(lambda req, n: error(req, FetchErrorKind.CONNECTION, "refused")),
    )
    await advance(engine, clock, 60 * 6, step=10)
    assert engine.online is True
    b = await bookmark(client, bid)
    assert b["status"] == "error" and b["consecutive_errors"] >= 3
    assert len(toasts.shown) == 1  # the one "is failing" notification


# -- resume: waiting for the network ----------------------------------------------------------


async def test_after_a_resume_dispatch_waits_for_the_network_then_catches_up(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, net: FakeNetwork
) -> None:
    ids = [await add(client, f"s{i}", schedule={"interval_s": 900, "jitter_pct": 0})
           for i in range(3)]  # fmt: skip
    await settle(engine, clock)
    net.up = False  # Wi-Fi is not back yet when the lid opens
    clock.jump_wall(2 * 3600)
    await advance(engine, clock, 5, step=5)
    assert "resume" in engine.scheduler.holds and engine.online is True
    await advance(engine, clock, 30, step=5)
    for bid in ids:  # nothing was dispatched into the void
        assert len(await runs(client, bid)) == 1
    assert net.probes >= 5  # it kept asking

    net.up = True
    await advance(engine, clock, 10, step=1)
    assert "resume" not in engine.scheduler.holds
    await advance(engine, clock, 10, step=1)
    await engine.drain_background()
    for bid in ids:
        rs = await runs(client, bid)
        assert [r["trigger"] for r in rs] == ["schedule", "catchup"]


async def test_a_network_that_never_returns_after_a_resume_means_offline_mode(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, net: FakeNetwork
) -> None:
    await engine.update_settings({"resume_probe_window_s": 20})
    bid = await add(client, "late", schedule={"interval_s": 900, "jitter_pct": 0})
    await settle(engine, clock)
    net.up = False
    clock.jump_wall(3600)
    await advance(engine, clock, 5, step=5)
    await advance(engine, clock, 30, step=5)  # past the 20 s window
    assert engine.online is False and "resume" not in engine.scheduler.holds
    assert len(await runs(client, bid)) == 1  # still nothing was dispatched, nothing counted
    net.up = True
    await advance(engine, clock, 20, step=5)  # the recovery probe (every 15 s) finds it
    await engine.drain_background()
    await advance(engine, clock, 10, step=1)
    assert engine.online is True
    assert [r["trigger"] for r in await runs(client, bid)] == ["schedule", "catchup"]


async def test_an_os_resume_message_and_the_drift_check_are_one_resume(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, power: NullPowerBackend
) -> None:
    await add(client, "x", schedule={"interval_s": 900, "jitter_pct": 0})
    await settle(engine, clock)
    assert power.started
    power.send("suspend")
    clock.jump_wall(3600)
    power.send("resume")  # WM_POWERBROADCAST arrives first...
    await clock.settle()  # (it is delivered through call_soon_threadsafe)
    await engine.drain_background()
    assert engine.power is not None and engine.power.resumes == 1
    await advance(engine, clock, 5, step=5)  # ...then the heartbeat sees the same wake
    assert engine.power.resumes == 1


async def test_a_logoff_message_stops_the_engine(engine: Engine, power: NullPowerBackend) -> None:
    power.send("shutdown")  # WM_QUERYENDSESSION
    await asyncio.sleep(0.01)  # (it is delivered through call_soon_threadsafe)
    assert engine._stopped.is_set()


async def test_catch_up_does_not_check_a_bookmark_outside_its_window(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    """Window 07:00-23:00 UTC; the machine sleeps through the night and wakes at 03:00."""
    sched = {"interval_s": 900, "jitter_pct": 0, "window": {"start": "07:00", "end": "23:00"}}
    bid = await add(client, "daytime", schedule=sched)
    free = await add(client, "anytime", schedule={"interval_s": 900, "jitter_pct": 0})
    await settle(engine, clock)
    clock.set_wall(datetime(2026, 1, 6, 3, 0, tzinfo=UTC))  # the next morning, 03:00
    await advance(engine, clock, 5, step=5)
    await advance(engine, clock, 10, step=1)
    await engine.drain_background()
    assert len(await runs(client, free)) == 2  # no window: caught up
    assert len(await runs(client, bid)) == 1  # not checked at 03:00 ...
    due = (await bookmark(client, bid))["next_due_at"]
    assert at(due) == datetime(2026, 1, 6, 7, 0, tzinfo=UTC)  # ... but at the window's start
    await advance(engine, clock, 4 * 3600 + 120, step=60)
    assert len(await runs(client, bid)) == 2


# -- restart ----------------------------------------------------------------------------------


async def test_a_restart_after_hours_staggers_what_became_overdue(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, data_dir: DataDir,
    settings_overrides: dict[str, Any], toasts: LogToastBackend, power: NullPowerBackend,
    net: FakeNetwork,
) -> None:  # fmt: skip
    ids = [await add(client, f"r{i}", schedule={"interval_s": 900, "jitter_pct": 0})
           for i in range(8)]  # fmt: skip
    await settle(engine, clock)
    await engine.stop()
    clock.advance(6 * 3600)  # the engine was not running for six hours

    again = make_engine(data_dir, clock, settings_overrides, toasts, power, net)
    await again.start()
    try:
        await settle(again, clock)  # the first of them starts right away
        await advance(again, clock, 12, step=1)
        for bid in ids:
            rs = await runs_via(again, bid)
            assert [r["trigger"] for r in rs] == ["schedule", "catchup"], rs
        started = {at(r["started_at"]) for bid in ids for r in (await runs_via(again, bid))[1:]}
        assert len(started) == 8  # one per second, not a stampede
    finally:
        await again.stop()


async def runs_via(engine: Engine, bid: int) -> list[dict[str, Any]]:
    from pagewatch.engine.store import repo

    rows = await engine.db.read(lambda c: repo.check_runs_for(c, bid, 100))
    return [dict(r) for r in reversed(rows)]


# -- battery ----------------------------------------------------------------------------------


async def test_battery_policy_pause_holds_a_bookmark_and_slow_stretches_its_interval(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, power: NullPowerBackend
) -> None:
    keep = await add(client, "keep")
    paused = await add(client, "paused", schedule={**SCHED, "on_battery": "pause"})
    slow = await add(client, "slow", schedule={**SCHED, "on_battery": "slow"})
    await settle(engine, clock)

    power.battery = True
    assert engine.power is not None
    engine.power.poll_battery()  # (the engine does this once a minute)
    assert engine.on_battery is True
    assert (await client.get("/health")).json()["on_battery"] is True
    await advance(engine, clock, 600, step=10)

    assert len(await runs(client, keep)) >= 10  # normal: unaffected
    assert len(await runs(client, paused)) == 1  # paused: nothing since it went on battery
    assert len(await runs(client, slow)) == 4  # first + checks at +60 s, +300 s, +540 s (x4)

    power.battery = False
    engine.power.poll_battery()
    await engine.drain_background()
    await advance(engine, clock, 30, step=1)
    rs = await runs(client, paused)
    assert [r["trigger"] for r in rs] == ["schedule", "catchup"]  # once, not ten times


async def test_the_battery_is_read_once_a_minute(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, power: NullPowerBackend
) -> None:
    await advance(engine, clock, 10, step=5)
    assert engine.on_battery is False
    power.battery = True
    await advance(engine, clock, 65, step=5)
    assert engine.on_battery is True


async def test_battery_saver_pauses_everything_only_when_the_option_is_on(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, power: NullPowerBackend
) -> None:
    bid = await add(client, "saver")
    await settle(engine, clock)
    power.saver = True
    assert engine.power is not None
    engine.power.poll_battery()
    await advance(engine, clock, 180, step=10)
    assert len(await runs(client, bid)) > 2  # the option is off by default: no effect

    await client.put("/settings", json={"pause_on_battery_saver": True})
    held = len(await runs(client, bid))
    await advance(engine, clock, 180, step=10)
    assert "saver" in engine.scheduler.holds
    assert len(await runs(client, bid)) == held
    power.saver = False
    engine.power.poll_battery()
    await advance(engine, clock, 120, step=10)
    assert len(await runs(client, bid)) > held


async def test_keep_awake_follows_the_option_and_autowatch(
    client: httpx.AsyncClient, engine: Engine, power: NullPowerBackend
) -> None:
    assert power.awake_calls[-1] is False  # off by default: the engine never prevents sleep
    await client.put("/settings", json={"keep_awake": True})
    assert power.awake_calls[-1] is True
    await client.post("/autowatch", json={"state": "paused"})
    assert power.awake_calls[-1] is False  # paused AutoWatch does not keep the machine awake
    await client.post("/autowatch", json={"state": "running"})
    assert power.awake_calls[-1] is True


# -- errors: opening a bookmark resets the counter ---------------------------------------------


async def test_marking_a_failing_bookmark_read_clears_its_error_state(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, net: FakeNetwork
) -> None:
    bid = await add(client, "gone")
    await settle(engine, clock)
    engine.fetchers.replace(
        "static",
        ScriptedFetcher(lambda req, n: error(req, FetchErrorKind.PARSE, "boom")),
    )
    await advance(engine, clock, 200, step=10)
    assert (await bookmark(client, bid))["status"] == "error"
    r = await client.post(f"/bookmarks/{bid}/read")
    assert r.status_code == 200
    b = await bookmark(client, bid)
    assert b["status"] == "ok" and b["consecutive_errors"] == 0


# -- a catastrophic regex cannot wedge the engine -------------------------------------------------


async def test_a_catastrophic_regex_fails_its_own_check_and_the_engine_carries_on(
    data_dir: DataDir, clock: FakeClock, toasts: LogToastBackend, power: NullPowerBackend,
    net: FakeNetwork,
) -> None:  # fmt: skip
    """A user regex with exponential backtracking would hang a worker for hours. With a real
    worker process the time limit kills it, the check fails (``processing timed out``), the
    pool is rebuilt, and other bookmarks are checked as before."""
    overrides = {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
        "worker_processes": 1, "toast_coalesce_s": 0.0, "worker_job_timeout_s": 4.0,
    }  # fmt: skip
    eng = Engine(
        data_dir, clock=clock, worker_mode="process", settings_overrides=overrides,
        toast_backend=toasts, rng=random.Random(3),
    )  # fmt: skip
    evil = "a" * 40 + "!"
    eng.fetchers.replace(
        "static",
        ScriptedFetcher(lambda req, n: ok(req, f"<html><body><p>{evil}</p></body></html>")),
    )
    await eng.start()
    server = ApiServer(eng)
    port = await server.start()
    write_lockfile(data_dir, LockInfo(pid=os.getpid(), port=port, token=eng.token,
                                      version="t", started_at="now"))  # fmt: skip
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {eng.token}"},
            timeout=60,
        ) as c:
            evil_filter = {
                "ignore": [{"type": "text", "pattern": "(a+)+$", "pattern_kind": "regex"}]
            }
            bad = await add(c, "evil", filter=evil_filter)
            await settle(eng, clock)
            rs = await runs(c, bad)
            assert rs[0]["outcome"] == "error" and rs[0]["reason"] == "parse", rs
            assert eng.pool.timeouts == 1 and eng.pool.restarts >= 1

            good = await add(c, "fine")  # the rebuilt pool serves the next bookmark normally
            await settle(eng, clock)
            assert [r["outcome"] for r in await runs(c, good)] == ["first"]
            assert (await c.get("/health")).status_code == 200
    finally:
        await server.stop()
        await eng.stop()
