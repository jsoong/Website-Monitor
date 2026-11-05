"""Spec, *Scheduler → Capacity*: "When a host's demand exceeds its limit, ... the scheduler stretches
that host's intervals evenly instead of letting the queue grow." Real engine, fake clock, fake
network, a real 2 s spacing per host (so one host serves at most 30 checks a minute)."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock, parse_iso
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from tests.support.fakes import ScriptedFetcher, ok
from tests.support.m5 import add, bookmark
from tests.support.sim import advance

PAGE = "<html><body><p>Nothing new has been posted to this page yet, check again later.</p></body></html>"
BUSY, QUIET = "busy.example.test", "quiet.example.test"


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    # the shared defaults switch the per-host spacing off; here it is the spec's 2 s
    return {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 2.0, "per_host_concurrency": 16,
        "worker_processes": 2, "toast_coalesce_s": 0.0,
    }  # fmt: skip


def new_engine(
    data_dir: DataDir, clock: FakeClock, overrides: dict[str, Any], toasts: LogToastBackend
) -> Engine:
    eng = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=overrides,
                 toast_backend=toasts, rng=random.Random(4))  # fmt: skip
    eng.fetchers.replace("static", ScriptedFetcher(lambda req, _n: ok(req, PAGE)))
    return eng


@pytest.fixture
async def engine(
    data_dir: DataDir, clock: FakeClock, settings_overrides: dict[str, Any], toasts: LogToastBackend
) -> AsyncIterator[Engine]:
    eng = new_engine(data_dir, clock, settings_overrides, toasts)
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def fill(client: httpx.AsyncClient, busy: int, quiet: int = 0) -> list[int]:
    """``busy`` bookmarks on one host and ``quiet`` on another, each asking for a check a minute."""
    ids = [await add(client, f"b{i}", url=f"http://{BUSY}/{i}") for i in range(busy)]
    ids += [await add(client, f"q{i}", url=f"http://{QUIET}/{i}") for i in range(quiet)]
    return ids


async def gap_s(client: httpx.AsyncClient, bid: int) -> float:
    """Seconds between a bookmark's last check and the one it is now waiting for."""
    b = await bookmark(client, bid)
    return (parse_iso(b["next_due_at"]) - parse_iso(b["last_checked_at"])).total_seconds()


# -- what it does ----------------------------------------------------------------------------


async def test_an_over_limit_host_is_stretched_evenly_and_a_quiet_host_is_left_alone(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    ids = await fill(client, busy=60, quiet=3)  # busy asks for 60 a minute; it can serve 30
    await engine.host_stretch.refresh()
    assert engine.host_stretch.factors == {BUSY: pytest.approx(2.0)}
    assert engine.host_stretch.factor(QUIET) == 1.0

    await advance(engine, clock, 130, step=2)  # 60 first checks, one per 2 s: all have had theirs
    busy_gaps = [await gap_s(client, i) for i in ids[:60]]
    quiet_gaps = [await gap_s(client, i) for i in ids[60:]]
    assert busy_gaps == [pytest.approx(120.0, abs=1.0)] * 60  # 2 x 60 s, all of them the same
    assert quiet_gaps == [pytest.approx(60.0, abs=1.0)] * 3

    # the capacity page still reports what the schedules ask for, not the stretched figure
    hosts = {h["host"]: h for h in (await client.get("/capacity")).json()["hosts"]}
    assert hosts[BUSY]["demand_per_min"] == pytest.approx(60.0) and hosts[BUSY]["over_limit"]
    assert not hosts[QUIET]["over_limit"]


async def test_the_queue_stays_empty_where_it_filled_up_without_the_stretch(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await fill(client, busy=60)
    await engine.host_stretch.refresh()
    await advance(engine, clock, 600, step=2)  # ten minutes: 600 checks asked for, 300 possible
    stretched = engine.scheduler.queue_length

    # The same load with the feature switched off. A bookmark is not rescheduled until its check
    # has finished, so the queue cannot pass the number of bookmarks: it settles at about half of
    # them waiting for a slot (every check a minute late) instead of growing without bound.
    engine.host_stretch._factors.clear()  # noqa: SLF001
    monkeypatch.setattr(engine.host_stretch, "refresh_soon", lambda **_kw: None)
    await advance(engine, clock, 600, step=2)
    unstretched = engine.scheduler.queue_length
    assert stretched <= 5, f"{stretched} checks waiting with the stretch"
    assert unstretched >= 20, (
        f"only {unstretched} waiting without it: the comparison proves nothing"
    )


async def test_the_factor_falls_back_when_demand_falls(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    ids = await fill(client, busy=60)
    await engine.host_stretch.refresh()
    assert engine.host_stretch.factor(BUSY) == pytest.approx(2.0)
    for bid in ids[:40]:
        assert (await client.delete(f"/bookmarks/{bid}")).status_code == 204
    await engine.host_stretch.refresh()  # 20 a minute left: under the 30 the host can serve
    assert engine.host_stretch.factors == {}

    await advance(engine, clock, 50, step=2)  # 20 first checks, one per 2 s
    assert [await gap_s(client, i) for i in ids[40:]] == [pytest.approx(60.0, abs=1.0)] * 20


async def test_changing_a_host_limit_in_settings_re_measures_at_once(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await fill(client, busy=60)
    await engine.host_stretch.refresh()
    assert engine.host_stretch.factor(BUSY) == pytest.approx(2.0)
    r = await client.put("/settings", json={"host_overrides": {BUSY: {"min_gap_s": 1.0}}})
    assert r.status_code == 200, r.text  # now it can serve 60 a minute: exactly what is asked
    await engine.drain_background()
    assert engine.host_stretch.factors == {}


async def test_a_restart_applies_the_stretch_from_the_first_reschedule(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, data_dir: DataDir,
    settings_overrides: dict[str, Any], toasts: LogToastBackend,
) -> None:  # fmt: skip
    await fill(client, busy=60)
    await engine.stop()
    again = new_engine(data_dir, clock, settings_overrides, toasts)
    await again.start()
    try:
        assert again.host_stretch.factors == {BUSY: pytest.approx(2.0)}  # no wait for a refresh
    finally:
        await again.stop()


# -- how often it measures ---------------------------------------------------------------------


async def test_checks_trigger_at_most_one_measurement_a_minute_however_many_finish_together(
    engine: Engine, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[int] = []

    async def counting() -> None:
        runs.append(1)

    monkeypatch.setattr(engine.host_stretch, "refresh", counting)
    engine.host_stretch._last = clock.monotonic()  # noqa: SLF001 - measured just now
    for _ in range(5):
        engine.host_stretch.refresh_soon()
    await engine.drain_background()
    assert runs == []  # too soon

    clock.advance(61)
    for _ in range(5):  # five checks finish in the same instant
        engine.host_stretch.refresh_soon()
    await engine.drain_background()
    assert runs == [1]  # one measurement, not five

    engine.host_stretch._last = clock.monotonic()  # noqa: SLF001
    engine.host_stretch.refresh_soon(force=True)  # a settings change does not wait
    await engine.drain_background()
    assert runs == [1, 1]


async def test_a_failed_measurement_keeps_the_old_factors_and_does_not_raise(
    client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await fill(client, busy=60)
    await engine.host_stretch.refresh()
    before = engine.host_stretch.factors
    assert before

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr("pagewatch.engine.capacity.tally", boom)
    await engine.host_stretch.refresh()  # logs a warning; the scheduler goes on with what it had
    assert engine.host_stretch.factors == before
    assert not engine.host_stretch._running  # noqa: SLF001 - and a later refresh can run
