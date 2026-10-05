"""The nightly chores: when they are due, that they survive restarts, and that a failure does
not become a retry storm."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pagewatch.engine.clock import FakeClock, iso
from pagewatch.engine.core import Engine
from pagewatch.engine.maintenance import RETRY_AFTER_S, Maintenance


def utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.fixture
async def m(engine: Engine) -> Maintenance:
    await engine.update_settings({"timezone": "UTC"})
    return Maintenance(engine)


# -- when a job is due ------------------------------------------------------------------------


async def test_a_job_is_due_once_the_slot_has_passed_since_it_last_ran(m: Maintenance) -> None:
    now = utc(2026, 3, 10, 14, 0)
    m.last["backup"] = utc(
        2026,
        3,
        10,
        3,
        0,
    ) + timedelta(seconds=5)  # ran today's slot
    assert not m.due("backup", now)
    m.last["backup"] = utc(
        2026,
        3,
        9,
        3,
        0,
    ) + timedelta(seconds=5)  # ran yesterday's
    assert m.due("backup", now)  # today's 03:00 was missed: at the next wake
    assert not m.due("backup", utc(2026, 3, 10, 2, 59))  # before today's slot: yesterday's is done


async def test_a_job_that_never_ran_is_due(m: Maintenance) -> None:
    assert m.due("backup", utc(2026, 3, 10, 14, 0)) and m.due(
        "maintenance", utc(2026, 3, 10, 14, 0)
    )


async def test_the_slot_is_the_most_recent_local_occurrence(m: Maintenance, engine: Engine) -> None:
    assert m.slot(utc(2026, 3, 10, 14, 0), "03:00") == utc(2026, 3, 10, 3, 0)
    assert m.slot(utc(2026, 3, 10, 2, 0), "03:00") == utc(2026, 3, 9, 3, 0)
    assert m.slot(utc(2026, 3, 10, 3, 0), "03:00") == utc(2026, 3, 10, 3, 0)  # exactly on it
    await engine.update_settings({"timezone": "America/New_York"})
    # 03:00 in New York (EDT, UTC-4, after 8 March) is 07:00 UTC
    assert m.slot(utc(2026, 3, 10, 12, 0), "03:00") == utc(2026, 3, 10, 7, 0)
    assert m.slot(utc(2026, 3, 10, 6, 0), "03:00") == utc(2026, 3, 9, 7, 0)
    # before the clocks went forward (EST, UTC-5) the same local time is 08:00 UTC
    assert m.slot(utc(2026, 3, 7, 12, 0), "03:00") == utc(2026, 3, 7, 8, 0)


async def test_the_backup_can_be_switched_off_but_retention_cannot(
    m: Maintenance, engine: Engine
) -> None:
    await engine.update_settings({"backup_enabled": False})
    assert not m.due("backup", utc(2026, 3, 10, 14, 0)) and m.due(
        "maintenance", utc(2026, 3, 10, 14, 0)
    )


async def test_the_times_are_configurable(m: Maintenance, engine: Engine) -> None:
    await engine.update_settings({"backup_time": "22:15"})
    m.last["backup"] = utc(2026, 3, 10, 3, 0)
    assert m.due("backup", utc(2026, 3, 10, 23, 0)) and not m.due("backup", utc(2026, 3, 10, 22, 0))


# -- running ----------------------------------------------------------------------------------


async def test_a_backup_run_writes_an_automatic_zip_prunes_and_remembers(
    m: Maintenance, engine: Engine, clock: FakeClock
) -> None:
    await engine.update_settings({"backup_keep": 2})
    for _ in range(4):
        assert await m.run("backup")
        clock.advance(86400)
    zips = sorted(engine.data_dir.backups_dir.glob("backup-auto-*.zip"))
    assert len(zips) == 2  # only the newest two
    assert m.last["backup"] is not None
    assert not m.due(
        "backup", m.last["backup"] + timedelta(seconds=1)
    )  # not due right after it ran
    assert m.due("backup", clock.now())  # but the next night's slot has since passed
    again = Maintenance(engine)  # a restart does not forget
    await again.start()
    try:
        assert again.last["backup"] == m.last["backup"] and again.last["maintenance"] is None
    finally:
        await again.stop()


async def test_a_retention_run_is_recorded(m: Maintenance, engine: Engine) -> None:
    assert await m.run("maintenance")
    assert m.last_retention is not None and m.last["maintenance"] is not None
    assert await engine.settings_store.get_state("maintenance_last") == iso(m.last["maintenance"])


async def test_state_is_not_a_setting(engine: Engine) -> None:
    await engine.settings_store.set_state("backup_last", "2026-03-10T03:00:00.000000Z")
    await engine.settings_store.load()
    assert "_state.backup_last" not in engine.settings.model_dump()
    assert await engine.settings_store.get_state("backup_last") == "2026-03-10T03:00:00.000000Z"
    await engine.update_settings({"keep_awake": True})  # and updating settings leaves it alone
    assert await engine.settings_store.get_state("backup_last") == "2026-03-10T03:00:00.000000Z"


async def test_a_failing_backup_reports_a_problem_and_waits_an_hour_before_trying_again(
    m: Maintenance, engine: Engine, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = engine.events.subscribe()

    async def boom(**kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(engine, "backup_now", boom)
    assert await m.run("backup") is False
    event = sub.queue.get_nowait()
    while event.type != "problem":
        event = sub.queue.get_nowait()
    assert event.data["kind"] == "backup_failed" and "disk full" in event.data["message"]
    assert m.last["backup"] is None
    assert not m.due("backup", clock.now())  # not hammered every minute
    clock.advance(RETRY_AFTER_S + 1)
    assert m.due("backup", clock.now())
    sub.close()
