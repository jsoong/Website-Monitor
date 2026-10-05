"""The durable action queue: order, retries, permanent failure, and surviving restarts.

The e-mail, export and ntfy actions do not exist until M6, so their names are used here as three
independent slots with recording stand-ins.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.base import AlertContext
from pagewatch.engine.actions.builtin import ACTIONS
from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock, parse_iso
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from tests.support.fakes import ScriptedFetcher, ok
from tests.support.m5 import add, bookmark
from tests.support.sim import advance, settle

A = "<html><body><p>Opening hours are posted weekly on this page for everyone.</p></body></html>"
B = "<html><body><p>Opening hours are posted weekly on this page for everyone.</p><p>New: the library is closed on Monday.</p></body></html>"


class Recorder:
    """Stands in for actions: logs start/end, and can be told to fail or hang."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self.times: dict[str, list[datetime]] = {}  # when each slot's action was called
        self.log: list[tuple[str, str]] = []  # (slot, "start" | "end" | "fail")
        self.fail: dict[str, int] = {}  # slot -> failures left (-1 = forever)
        self.delay: dict[str, float] = {}
        self.hang: set[str] = set()
        self.calls: dict[str, int] = {}

    def action(self, slot: str) -> Any:
        async def run(engine: Engine, ctx: AlertContext) -> None:
            self.calls[slot] = self.calls.get(slot, 0) + 1
            if self.clock is not None:
                self.times.setdefault(slot, []).append(self.clock.now())
            self.log.append((slot, "start"))
            if slot in self.hang:
                await asyncio.sleep(3600)
            if self.delay.get(slot):
                await asyncio.sleep(self.delay[slot])
            left = self.fail.get(slot, 0)
            if left != 0:
                self.fail[slot] = left - 1 if left > 0 else left
                self.log.append((slot, "fail"))
                raise RuntimeError(f"{slot} is down")
            self.log.append((slot, "end"))

        return run

    def order(self) -> list[str]:
        return [f"{s}:{e}" for s, e in self.log]


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Recorder:
    r = Recorder(clock)
    for slot in ("email", "export", "ntfy"):
        monkeypatch.setitem(ACTIONS, slot, r.action(slot))
    return r


class Page:
    def __init__(self) -> None:
        self.body = A

    def fetcher(self) -> ScriptedFetcher:
        return ScriptedFetcher(lambda req, n: ok(req, self.body))


@pytest.fixture
def page() -> Page:
    return Page()


@pytest.fixture
async def engine(
    data_dir: DataDir, clock: FakeClock, settings_overrides: dict[str, Any],
    toasts: LogToastBackend, page: Page,
) -> AsyncIterator[Engine]:  # fmt: skip
    eng = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
                 toast_backend=toasts, rng=random.Random(5))  # fmt: skip
    eng.fetchers.replace("static", page.fetcher())
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def alert(client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page,
                *slots: str) -> int:  # fmt: skip
    """A bookmark with these actions, checked once, then changed so that it alerts."""
    actions = {"actions": [{"type": s} for s in slots]}
    bid = await add(client, "lib", actions=actions)
    await settle(engine, clock)
    page.body = B
    await advance(engine, clock, 70, step=10)
    return bid


async def jobs(engine: Engine) -> list[dict[str, Any]]:
    rows = await engine.db.read(
        lambda c: c.execute("SELECT * FROM action_job ORDER BY action_index").fetchall()
    )
    return [dict(r) for r in rows]


# -- order ------------------------------------------------------------------------------------


async def test_a_changes_jobs_run_one_at_a_time_in_order_with_mark_read_last(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder
) -> None:
    rec.delay["email"] = 0.05
    # configured with mark_read FIRST: the spec says it always runs last
    bid = await alert(client, engine, clock, page, "mark_read", "email", "export", "ntfy")
    assert [j["action_type"] for j in await jobs(engine)] == [
        "email",
        "export",
        "ntfy",
        "mark_read",
    ]
    assert rec.order() == ["email:start", "email:end", "export:start", "export:end",
                           "ntfy:start", "ntfy:end"]  # fmt: skip
    assert [j["status"] for j in await jobs(engine)] == ["done"] * 4
    b = await bookmark(client, bid)
    assert b["unread"] is False  # mark_read really ran, after the others


async def test_a_job_waits_while_an_earlier_one_is_waiting_for_its_retry(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder
) -> None:
    rec.fail["email"] = 1
    await alert(client, engine, clock, page, "email", "export")
    js = await jobs(engine)
    assert [j["status"] for j in js] == ["queued", "queued"] and rec.calls == {"email": 1}
    assert js[0]["attempts"] == 1 and "email is down" in js[0]["last_error"]
    await advance(engine, clock, 61, step=1)  # the first back-off is one minute
    assert [j["status"] for j in await jobs(engine)] == ["done", "done"]
    assert rec.order() == ["email:start", "email:fail", "email:start", "email:end",
                           "export:start", "export:end"]  # fmt: skip


# -- retries ----------------------------------------------------------------------------------


async def test_retries_back_off_one_five_then_thirty_minutes(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder
) -> None:
    rec.fail["email"] = 3  # fails three times, the fourth attempt works
    await alert(client, engine, clock, page, "email")
    for n, backoff in enumerate((60, 300, 1800), start=1):
        assert rec.calls["email"] == n
        job = (await jobs(engine))[0]
        assert job["status"] == "queued" and job["attempts"] == n
        # the schedule is exact: this attempt's time plus 1, 5, then 30 minutes
        due = parse_iso(job["next_attempt_at"])
        assert due - rec.times["email"][-1] == timedelta(seconds=backoff)
        # not before the deadline ...
        await advance(engine, clock, (due - clock.now()).total_seconds() - 1, step=5)
        assert rec.calls["email"] == n
        # ... and promptly after it
        await advance(engine, clock, 6, step=1)
        assert rec.calls["email"] == n + 1
    assert (await jobs(engine))[0]["status"] == "done"


async def test_the_fifth_failure_is_final_and_is_reported(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder
) -> None:
    rec.fail["email"] = -1
    sub = engine.events.subscribe()
    bid = await alert(client, engine, clock, page, "email", "export", "mark_read")
    await advance(engine, clock, (1 + 5 + 30 + 30) * 60 + 120, step=30)
    js = await jobs(engine)
    email, export, mark = js
    assert email["status"] == "failed" and email["attempts"] == 5
    assert "email is down" in email["last_error"]
    assert export["status"] == "done"  # the failure does not block the later actions
    assert rec.calls["email"] == 5  # and it is not tried a sixth time
    # but a change the user was never told about must not be marked read
    assert mark["status"] == "done" and "stays unread" in mark["last_error"]
    assert (await bookmark(client, bid))["unread"] is True
    kinds = []
    while not sub.queue.empty():
        e = sub.queue.get_nowait()
        if e.type == "problem":
            kinds.append(e.data["kind"])
    assert kinds == ["action_failed"]  # once, with the last error
    sub.close()


# -- durability -------------------------------------------------------------------------------


async def test_queued_jobs_survive_a_restart_and_run_when_their_time_comes(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder,
    data_dir: DataDir, settings_overrides: dict[str, Any], toasts: LogToastBackend,
) -> None:  # fmt: skip
    rec.fail["email"] = 1
    await alert(client, engine, clock, page, "email", "export")
    assert [j["status"] for j in await jobs(engine)] == ["queued", "queued"]
    await engine.stop()

    again = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
                   toast_backend=toasts, rng=random.Random(5))  # fmt: skip
    again.fetchers.replace("static", page.fetcher())
    await again.start()
    try:
        await advance(again, clock, 30, step=10)
        assert rec.calls["email"] == 1  # not yet: its retry is a minute after the failure
        await advance(again, clock, 60, step=10)
        assert [j["status"] for j in await jobs(again)] == ["done", "done"]
        calls = dict(rec.calls)
        await again.stop()
        third = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
                       toast_backend=toasts, rng=random.Random(5))  # fmt: skip
        third.fetchers.replace("static", page.fetcher())
        await third.start()
        await advance(third, clock, 120, step=30)
        assert rec.calls == calls  # nothing that was done is sent again
        await third.stop()
    finally:
        await again.stop()


async def test_a_job_interrupted_by_a_crash_runs_again_exactly_once(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder,
    data_dir: DataDir, settings_overrides: dict[str, Any], toasts: LogToastBackend,
) -> None:  # fmt: skip
    rec.hang.add("email")  # the engine goes away while the e-mail is being sent
    await add(client, "lib", actions={"actions": [{"type": "email"}, {"type": "export"}]})
    await settle(engine, clock)
    page.body = B
    clock.advance(70)
    await engine.scheduler.wait_idle()
    for _ in range(200):  # (the queue never goes idle with a hung job: wait for it to start)
        if rec.calls.get("email"):
            break
        await asyncio.sleep(0.01)
    assert rec.order() == ["email:start"]
    await engine.stop()
    assert [j["status"] for j in await jobs_after_stop(data_dir)] == ["queued", "queued"]

    rec.hang.clear()
    again = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
                   toast_backend=toasts, rng=random.Random(5))  # fmt: skip
    again.fetchers.replace("static", page.fetcher())
    await again.start()
    try:
        await advance(again, clock, 30, step=10)
        assert rec.order() == [
            "email:start",
            "email:start",
            "email:end",
            "export:start",
            "export:end",
        ]
        assert [j["status"] for j in await jobs(again)] == ["done", "done"]
    finally:
        await again.stop()


async def jobs_after_stop(data_dir: DataDir) -> list[dict[str, Any]]:
    import sqlite3

    conn = sqlite3.connect(data_dir.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM action_job ORDER BY action_index")]
    finally:
        conn.close()


async def test_the_queue_wakes_for_a_retry_at_its_deadline_not_at_the_next_idle_poll(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder
) -> None:
    """The retry is due 60 s after the failure; the loop sleeps until then, not for a flat
    minute counted from whenever it last looked."""
    rec.fail["email"] = 1
    await alert(client, engine, clock, page, "email")
    failed_at = rec.times["email"][0]
    await advance(
        engine, clock, (failed_at + timedelta(seconds=59) - clock.now()).total_seconds(), step=1
    )
    assert rec.calls["email"] == 1
    await advance(engine, clock, 2, step=1)
    assert rec.calls["email"] == 2 and rec.times["email"][1] - failed_at <= timedelta(seconds=61)


async def test_a_deleted_bookmarks_jobs_disappear_and_do_not_run(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page, rec: Recorder
) -> None:
    rec.fail["email"] = -1
    bid = await alert(client, engine, clock, page, "email")
    assert len(await jobs(engine)) == 1
    assert (await client.delete(f"/bookmarks/{bid}")).status_code == 204
    await advance(engine, clock, 600, step=30)
    assert await jobs(engine) == [] and rec.calls["email"] == 1
