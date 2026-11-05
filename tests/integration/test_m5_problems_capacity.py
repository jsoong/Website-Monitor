"""M5 screens, engine side: ``GET /problems`` (the Problems list), ``GET /capacity`` (the Settings
capacity page) and removing a per-host override through ``PUT /settings``."""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.base import AlertContext
from pagewatch.engine.actions.builtin import ACTIONS
from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.capacity import checks_per_second
from pagewatch.engine.clock import FakeClock, iso, parse_iso
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from pagewatch.models import FetchErrorKind
from tests.support.fakes import ScriptedFetcher, error, ok
from tests.support.m5 import add, bookmark
from tests.support.sim import advance, settle

A = "<html><body><p>Opening hours are posted weekly on this page for everyone.</p></body></html>"
B = "<html><body><p>Opening hours are posted weekly on this page for everyone.</p><p>New: the library is closed on Monday.</p></body></html>"


class Page:
    """What the fake network answers: a page, or a certificate error."""

    def __init__(self) -> None:
        self.body = A
        self.broken = False

    def fetcher(self) -> ScriptedFetcher:
        def respond(req: Any, _n: int) -> Any:
            if self.broken:
                return error(req, FetchErrorKind.TLS, "certificate has expired")
            return ok(req, self.body)

        return ScriptedFetcher(respond)


@pytest.fixture
def page() -> Page:
    return Page()


@pytest.fixture
async def engine(
    data_dir: DataDir, clock: FakeClock, settings_overrides: dict[str, Any],
    toasts: LogToastBackend, page: Page,
) -> AsyncIterator[Engine]:  # fmt: skip
    eng = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
                 toast_backend=toasts, rng=random.Random(9))  # fmt: skip
    eng.fetchers.replace("static", page.fetcher())
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def problems(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    r = await client.get("/problems", params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# -- the Problems list --------------------------------------------------------------------------


async def test_nothing_is_wrong_and_the_token_is_required(
    client: httpx.AsyncClient, api: tuple[str, str]
) -> None:
    assert await problems(client) == {"items": [], "total": 0}
    async with httpx.AsyncClient(base_url=api[0]) as anonymous:
        assert (await anonymous.get("/problems")).status_code == 401
        assert (await anonymous.get("/capacity")).status_code == 401


async def test_a_failing_bookmark_is_listed_with_its_last_error_and_clears_when_it_recovers(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page
) -> None:
    bid = await add(client, "shop", gate={"error_threshold": 1})
    await settle(engine, clock)
    assert (await problems(client))["total"] == 0
    page.broken = True
    await advance(engine, clock, 70, step=10)

    out = await problems(client)
    assert out["total"] == 1
    p = out["items"][0]
    assert (p["kind"], p["bookmark_id"], p["bookmark_name"], p["message"]) == (
        "bookmark_error", bid, "shop", "tls",
    )  # fmt: skip
    assert p["consecutive_errors"] == 1 and p["job_id"] is None and p["url"].endswith(".test/")
    assert abs((parse_iso(p["occurred_at"]) - clock.now()).total_seconds()) < 120

    page.broken = False  # the page works again: the next good check clears it by itself
    await client.post(f"/bookmarks/{bid}/check")
    await settle(engine, clock)
    assert (await bookmark(client, bid))["status"] != "error"
    assert (await problems(client))["total"] == 0


async def test_opening_a_failing_bookmark_clears_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page
) -> None:
    bid = await add(client, "shop", gate={"error_threshold": 1})
    await settle(engine, clock)
    page.broken = True
    await advance(engine, clock, 70, step=10)
    assert (await problems(client))["total"] == 1
    assert (await client.post(f"/bookmarks/{bid}/read")).status_code == 200  # the M5 rule
    assert (await problems(client)) == {"items": [], "total": 0}


async def test_a_bookmark_that_needs_a_login_is_listed_as_such(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    bid = await add(client, "portal")
    await engine.db.write(
        lambda c: c.execute("UPDATE bookmark SET status='needs_login' WHERE id=?", (bid,))
    )
    (p,) = (await problems(client))["items"]
    assert (p["kind"], p["message"], p["occurred_at"]) == ("needs_login", "needs login", None)


async def test_newest_first_and_limit_keeps_the_true_total(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page
) -> None:
    first = await add(client, "first", gate={"error_threshold": 1})
    await settle(engine, clock)
    page.broken = True
    await advance(engine, clock, 70, step=10)  # `first` fails here
    second = await add(client, "second", gate={"error_threshold": 1})
    await advance(engine, clock, 70, step=10)  # `second` fails later
    out = await problems(client)
    assert [p["bookmark_id"] for p in out["items"]] == [second, first] and out["total"] == 2
    cut = await problems(client, limit=1)
    assert [p["bookmark_id"] for p in cut["items"]] == [second] and cut["total"] == 2
    assert (await client.get("/problems", params={"limit": 0})).status_code == 422


async def test_a_failed_action_is_listed_until_the_change_is_read(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, page: Page,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    async def down(engine: Engine, ctx: AlertContext) -> None:
        raise RuntimeError("smtp.example.test refused the connection")

    monkeypatch.setitem(ACTIONS, "email", down)
    bid = await add(client, "lib", actions={"actions": [{"type": "email"}]})
    await settle(engine, clock)
    page.body = B
    await advance(engine, clock, 70, step=10)  # the change alerts; the email action starts failing
    assert (await problems(client))["total"] == 0  # still retrying: not a problem yet
    await advance(engine, clock, (1 + 5 + 30 + 30) * 60 + 120, step=30)  # 5 attempts used up

    out = await problems(client)
    assert out["total"] == 1
    p = out["items"][0]
    assert (p["kind"], p["bookmark_id"], p["action_type"], p["attempts"]) == (
        "action_failed", bid, "email", 5,
    )  # fmt: skip
    assert "refused the connection" in p["message"]
    assert p["job_id"] and p["change_id"] and p["consecutive_errors"] is None

    await client.post(f"/bookmarks/{bid}/read")  # the user has seen the change
    assert (await problems(client)) == {"items": [], "total": 0}


# -- capacity ----------------------------------------------------------------------------------


def test_checks_per_second_for_each_schedule_mode() -> None:
    assert checks_per_second({"mode": "interval", "interval_s": 60}, None) == pytest.approx(1 / 60)
    assert checks_per_second({"mode": "manual", "interval_s": 60}, None) == 0.0
    assert checks_per_second({"mode": "times", "times": ["09:00", "18:00"]}, None) == (
        pytest.approx(2 / 86400)
    )
    assert checks_per_second({"mode": "times", "times": ["09:00"], "days": ["mon"]}, None) == (
        pytest.approx(1 / (7 * 86400))
    )
    adaptive = {"mode": "adaptive", "interval_s": 60, "adaptive": {"min_s": 900}}
    assert checks_per_second(adaptive, 1800) == pytest.approx(1 / 1800)  # its current interval
    assert checks_per_second(adaptive, None) == pytest.approx(1 / 900)  # not yet adapted
    assert checks_per_second({"mode": "nonsense"}, None) == 0.0  # an unreadable row never raises


async def capacity(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    r = await client.get("/capacity", params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def quiet(client: httpx.AsyncClient) -> None:
    """Pause AutoWatch so nothing runs while the test only looks at the numbers."""
    assert (await client.post("/autowatch", json={"state": "paused"})).status_code == 200


def pool(out: dict[str, Any], name: str) -> dict[str, Any]:
    (p,) = (p for p in out["pools"] if p["name"] == name)
    return p


async def test_demand_is_summed_per_pool_and_per_host_against_each_hosts_spacing(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await quiet(client)
    await client.put("/settings", json={"per_host_min_gap_s": 30.0})  # ceiling: 2 checks a minute
    for i in range(3):  # busy.test: 3 checks a minute asked of a host that allows 2
        await add(client, f"busy{i}", url="http://busy.test/", schedule={"interval_s": 60})
    await add(client, "calm", url="http://calm.test/", schedule={"interval_s": 120})
    await add(client, "by-hand", url="http://calm.test/x", schedule={"mode": "manual"})
    off = await add(client, "off", url="http://off.test/", schedule={"interval_s": 60})
    await client.patch(f"/bookmarks/{off}", json={"enabled": False})
    await add(client, "js", url="http://js.test/", check_method="browser")

    out = await capacity(client)
    static = pool(out, "static")
    assert static["bookmarks"] == 4  # three busy + one calm; manual and disabled do not count
    assert static["demand_per_s"] == pytest.approx(3 / 60 + 1 / 120)
    assert pool(out, "browser")["bookmarks"] == 1
    hosts = {h["host"]: h for h in out["hosts"]}
    assert set(hosts) == {"busy.test", "calm.test", "js.test"}  # no demand, no host
    busy, calm = hosts["busy.test"], hosts["calm.test"]
    assert busy["bookmarks"] == 3 and busy["demand_per_min"] == pytest.approx(3.0)
    assert busy["ceiling_per_min"] == pytest.approx(2.0) and busy["over_limit"] is True
    assert busy["utilisation"] == pytest.approx(1.5)
    assert calm["demand_per_min"] == pytest.approx(0.5) and calm["over_limit"] is False
    assert out["hosts"][0]["host"] == "busy.test"  # busiest first
    assert out["hosts_total"] == 3 and out["hosts_over_limit"] == 1


async def test_a_host_override_changes_that_hosts_ceiling(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await quiet(client)
    await client.put("/settings", json={"per_host_min_gap_s": 30.0})
    for i in range(3):
        await add(client, f"b{i}", url="http://busy.test/", schedule={"interval_s": 60})
    assert (await capacity(client))["hosts_over_limit"] == 1
    await client.put("/settings", json={"host_overrides": {"busy.test": {"min_gap_s": 10.0}}})
    out = await capacity(client)  # 10 s spacing: 6 a minute, so 3 is comfortable
    assert out["hosts"][0]["ceiling_per_min"] == pytest.approx(6.0) and out["hosts_over_limit"] == 0
    await client.put("/settings", json={"host_overrides": {"busy.test": {"min_gap_s": 0.0}}})
    host = (await capacity(client))["hosts"][0]
    assert host["ceiling_per_min"] is None and host["utilisation"] is None  # no spacing: no ceiling


async def seed_runs(engine: Engine, clock: FakeClock, method: str, ms: int, n: int) -> None:
    now = iso(clock.now())

    def write(conn: sqlite3.Connection) -> None:
        bid = conn.execute("SELECT id FROM bookmark LIMIT 1").fetchone()[0]
        conn.executemany(
            "INSERT INTO check_run(bookmark_id, started_at, finished_at, trigger, method, "
            "outcome, duration_ms) VALUES (?,?,?,?,?,?,?)",
            [(bid, now, now, "schedule", method, "unchanged", ms)] * n,
        )

    await engine.db.write(write)


@pytest.mark.parametrize(
    ("check_ms", "warn"),
    # one page, one bookmark a minute: 1.5x, 0.9x, 0.75x and 0.5x of the pool's capacity
    [(90_000, True), (54_000, True), (45_000, False), (30_000, False)],
)
async def test_browser_demand_is_compared_with_the_pool_and_warns_above_80_percent(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, check_ms: int, warn: bool
) -> None:
    await quiet(client)
    await client.put("/settings", json={"browser_pool": 1})
    await add(client, "js", url="http://js.test/", check_method="browser")
    before = pool(await capacity(client), "browser")
    assert before["capacity_per_s"] is None and before["warn"] is False  # nothing timed yet

    await seed_runs(engine, clock, "browser", check_ms, 6)
    after = pool(await capacity(client), "browser")
    assert after["samples"] == 6 and after["mean_check_s"] == pytest.approx(check_ms / 1000)
    assert after["capacity_per_s"] == pytest.approx(1 / (check_ms / 1000))
    assert after["utilisation"] == pytest.approx((1 / 60) / (1 / (check_ms / 1000)))
    assert after["warn"] is warn


async def test_too_few_timed_checks_give_no_capacity_figure(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    await quiet(client)
    await add(client, "js", url="http://js.test/", check_method="browser")
    await seed_runs(engine, clock, "browser", 5000, 4)  # fewer than the minimum sample
    p = pool(await capacity(client), "browser")
    assert p["samples"] == 4 and p["mean_check_s"] is None and p["capacity_per_s"] is None


async def test_old_checks_and_skipped_runs_do_not_count_towards_the_mean(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    await quiet(client)
    await add(client, "js", url="http://js.test/", check_method="browser")
    old = iso(clock.now() - timedelta(hours=30))
    now = iso(clock.now())

    def write(conn: sqlite3.Connection) -> None:
        bid = conn.execute("SELECT id FROM bookmark LIMIT 1").fetchone()[0]
        rows = [(bid, old, "browser", "unchanged", 60_000)] * 10  # too old
        rows += [(bid, now, "browser", "skipped", 60_000)] * 10  # took no pool time
        rows += [(bid, now, "browser", "unchanged", 2000)] * 5
        conn.executemany(
            "INSERT INTO check_run(bookmark_id, started_at, trigger, method, outcome, "
            "duration_ms) VALUES (?,?,'schedule',?,?,?)",
            rows,
        )

    await engine.db.write(write)
    p = pool(await capacity(client), "browser")
    assert p["samples"] == 5 and p["mean_check_s"] == pytest.approx(2.0)


async def test_capacity_for_ten_thousand_bookmarks_is_prompt_and_cuts_the_host_list(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await quiet(client)
    now = iso(engine.clock.now())
    n = 10_000

    def write(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO bookmark(name, url, schedule_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            [(f"b{i}", f"https://host{i % 400}.test/p/{i}", '{"interval_s":900}', now, now)
             for i in range(n)],
        )  # fmt: skip

    await engine.db.write(write)
    t0 = time.perf_counter()
    out = await capacity(client, hosts=10)
    elapsed = time.perf_counter() - t0
    assert pool(out, "static")["bookmarks"] == n
    assert pool(out, "static")["demand_per_s"] == pytest.approx(n / 900)
    assert len(out["hosts"]) == 10 and out["hosts_total"] == 400
    assert elapsed < 2.0, f"the capacity page took {elapsed:.2f}s for {n} bookmarks"


# -- settings: removing a host override ---------------------------------------------------------


async def test_null_removes_a_host_override_and_nothing_else(client: httpx.AsyncClient) -> None:
    body = {"host_overrides": {"a.test": {"min_gap_s": 5}, "b.test": {"concurrency": 1}}}
    assert (await client.put("/settings", json=body)).json()["host_overrides"].keys() == {
        "a.test", "b.test",
    }  # fmt: skip
    r = await client.put("/settings", json={"host_overrides": {"a.test": None}})
    assert r.status_code == 200 and set(r.json()["host_overrides"]) == {"b.test"}
    assert set((await client.get("/settings")).json()["host_overrides"]) == {"b.test"}
    await client.put("/settings", json={"host_overrides": {"b.test": {"min_gap_s": 1}}})
    assert (await client.get("/settings")).json()["host_overrides"]["b.test"] == {
        "concurrency": 1, "min_gap_s": 1.0,
    }  # a partial override still merges into the existing one  # fmt: skip


async def test_a_rejected_setting_names_the_field_so_a_form_can_show_it(
    client: httpx.AsyncClient,
) -> None:
    r = await client.put("/settings", json={"static_pool": 0})
    assert r.status_code == 422 and r.json()["detail"].startswith("static_pool: ")
    r = await client.put("/settings", json={"timezone": "Not/AZone"})
    assert r.status_code == 422 and r.json()["detail"].startswith("timezone: ")
    r = await client.put("/settings", json={"default_schedule": {"interval_s": 5}})
    assert r.status_code == 422 and r.json()["detail"].startswith("default_schedule.interval_s: ")
    r = await client.put("/settings", json={"nope": 1})
    assert r.status_code == 422 and "nope" in r.json()["detail"]  # unknown keys are still refused
