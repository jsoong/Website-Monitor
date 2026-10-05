from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock, iso
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    body = {"url": url, "schedule": {"interval_s": 60, "jitter_pct": 0}, **kw}
    r = await client.post("/bookmarks", json=body)
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return list(reversed(items))


# -- conditional GET --------------------------------------------------------------------


async def test_conditional_get_uses_etag_and_force_skips_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/e", article("steady"), etag=True)
    bid = await add(client, site.url("/e"))
    await settle(engine, clock)
    await advance(engine, clock, 70)
    first, second = site.hits_for("/e")[:2]
    assert "If-None-Match" not in first.headers and second.status == 304
    assert second.headers["If-None-Match"].startswith('"')
    assert [r["outcome"] for r in await runs(client, bid)] == ["first", "unchanged"]
    await client.post(f"/bookmarks/{bid}/check", params={"force": True})
    await settle(engine, clock)
    assert site.hits_for("/e")[2].status == 200  # force: unconditional request


# -- errors -----------------------------------------------------------------------------


async def test_transient_error_retries_once_before_counting_then_errors_out_with_one_toast(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/x", article("ok"))
    bid = await add(client, site.url("/x"))
    await settle(engine, clock)
    site.set("/x", "boom", status=503)

    await advance(engine, clock, 65, step=5)  # scheduled check fails -> a retry is queued
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert (
        b["consecutive_errors"] == 0 and b["status"] == "ok"
    )  # a transient error does not count yet
    await advance(engine, clock, 60, step=5)  # the retry also fails: now it counts
    assert (await client.get(f"/bookmarks/{bid}")).json()["consecutive_errors"] == 1
    rs = await runs(client, bid)
    assert [(r["trigger"], r["outcome"], r["reason"]) for r in rs[1:3]] == [
        ("schedule", "error", "http_503"), ("retry", "error", "http_503"),
    ]  # fmt: skip

    await advance(engine, clock, 600, step=10)  # keeps failing: threshold (3) reached
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["status"] == "error" and b["consecutive_errors"] >= 3
    error_toasts = [t for t in toasts.shown if "failing" in t.title]
    assert len(error_toasts) == 1 and not [t for t in toasts.shown if "failing" not in t.title]
    await advance(engine, clock, 900, step=15)
    assert (
        len([t for t in toasts.shown if "failing" in t.title]) == 1
    )  # notified once, not every check

    site.set("/x", article("ok"))  # recovery resets the counter and the status
    await advance(engine, clock, 200, step=10)
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["status"] == "ok" and b["consecutive_errors"] == 0
    assert not [
        t for t in toasts.shown if "failing" not in t.title
    ]  # recovery to the same text is no change


async def test_permanent_error_counts_immediately_without_a_retry(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await add(client, site.url("/missing"))  # 404
    await settle(engine, clock)
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["consecutive_errors"] == 1
    rs = await runs(client, bid)
    assert [r["reason"] for r in rs] == ["http_404"] and all(r["trigger"] != "retry" for r in rs)


async def test_connection_refused_and_too_large_and_unreachable_ftp_and_missing_file(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite, tmp_path: Path
) -> None:
    site.set("/big", "<p>" + "x" * 5000 + "</p>")
    dead = await add(client, "http://127.0.0.1:9/never", fetch={"timeout_s": 2})
    big = await add(client, site.url("/big"), fetch={"max_bytes": 2048})
    ftp = await add(client, "ftp://127.0.0.1:9/file", fetch={"timeout_s": 2})
    gone = await add(client, (tmp_path / "missing.txt").as_uri())
    await settle(engine, clock)
    reasons = {bid: (await runs(client, bid))[0]["reason"] for bid in (dead, big, ftp, gone)}
    assert reasons[dead] == "connection" and reasons[big] == "too_large"
    assert reasons[ftp] == "connection" and reasons[gone] == "http_404"


async def test_error_page_blacklist_does_not_replace_the_baseline_or_count_as_an_error(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/s", article("real content here"))
    bid = await add(client, site.url("/s"), gate={"blacklist": ["service unavailable"]})
    await settle(engine, clock)
    site.set("/s", article("Service Unavailable"))  # a 200 that is really an error page
    await advance(engine, clock, 130, step=10)
    rs = await runs(client, bid)
    assert {(r["outcome"], r["reason"]) for r in rs[1:]} == {("suppressed", "blacklist")}
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["consecutive_errors"] == 0 and b["status"] == "ok" and not toasts.shown
    site.set("/s", article("real content here"))  # the real page comes back: still no change
    await advance(engine, clock, 130, step=10)
    assert not toasts.shown


# -- politeness -------------------------------------------------------------------------


async def test_per_host_concurrency_is_capped(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await client.put("/settings", json={"per_host_concurrency": 2})
    for i in range(8):
        site.set(f"/c{i}", article(f"c{i}"), delay=0.05)
        await add(client, site.url(f"/c{i}"))
    await settle(engine, clock)
    assert len(site.hits) == 8 and site.max_in_flight == 2


async def test_per_host_spacing_and_overrides(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await client.put("/settings", json={"per_host_concurrency": 4, "per_host_min_gap_s": 2.0})
    for i in range(4):
        site.set(f"/g{i}", article(f"g{i}"))
        await add(client, site.url(f"/g{i}"))
    await clock.settle()
    await engine.scheduler.wait_idle()
    assert len(site.hits) == 1  # the first starts at once, the rest wait their 2 s turn
    for expected in (2, 3, 4):
        clock.advance(2.0)
        await clock.settle()
        await engine.scheduler.wait_idle()
        assert len(site.hits) == expected
    # a per-host override removes the spacing (the gap already committed by the last
    # request still has to elapse; after that all four go out at once)
    await client.put("/settings", json={"host_overrides": {"127.0.0.1": {"min_gap_s": 0}}})
    for i in range(4, 8):
        site.set(f"/g{i}", article(f"g{i}"))
        await add(client, site.url(f"/g{i}"))
    await clock.settle()
    await engine.scheduler.wait_idle()
    assert len(site.hits) == 4
    clock.advance(2.0)
    await settle(engine, clock)
    assert len(site.hits) == 8


async def test_retry_after_backs_the_whole_host_off(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/limited", "slow down", status=429, headers={"Retry-After": "120"})
    site.set("/other", article("fine"))
    await add(client, site.url("/limited"))
    await settle(engine, clock)
    assert len(site.hits_for("/limited")) == 1
    await add(client, site.url("/other"))  # same host: held back by the host-wide back-off
    await advance(engine, clock, 100, step=5)
    assert site.hits_for("/other") == []
    await advance(engine, clock, 30, step=5)
    assert len(site.hits_for("/other")) == 1


# -- autowatch, hotsites, coalescing ----------------------------------------------------


async def test_pause_stops_scheduled_checks_but_not_manual_ones_and_resume_catches_up(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/p", article("a"))
    bid = await add(client, site.url("/p"))
    await settle(engine, clock)
    r = await client.post("/autowatch", json={"state": "paused"})
    assert r.json() == {"state": "paused", "until": None}
    await advance(engine, clock, 300)
    assert len(site.hits) == 1  # nothing ran while paused, though checks are overdue
    assert (await client.post(f"/bookmarks/{bid}/check")).json()[
        "queued"
    ] == 1  # manual still works
    await settle(engine, clock)
    assert len(site.hits) == 2
    await advance(engine, clock, 300)
    assert len(site.hits) == 2  # ...and the schedule stays paused afterwards
    await client.post("/autowatch", json={"state": "running"})
    await advance(engine, clock, 70, step=5)
    assert len(site.hits) >= 3  # overdue work resumes
    stored = await engine.db.read(
        lambda c: c.execute("SELECT value_json FROM setting WHERE key='autowatch_state'").fetchone()
    )
    assert stored["value_json"] == '"running"'


async def test_timed_pause_ends_by_itself(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/t", article("a"))
    await add(client, site.url("/t"))
    await settle(engine, clock)
    until = iso(clock.now() + timedelta(minutes=10))
    assert (
        await client.post("/autowatch", json={"state": "paused", "until": until})
    ).status_code == 200
    assert (
        await client.post(
            "/autowatch", json={"state": "paused", "until": iso(clock.now() - timedelta(minutes=1))}
        )
    ).status_code == 422
    await advance(engine, clock, 500, step=20)
    assert len(site.hits) == 1
    await advance(engine, clock, 200, step=20)
    assert len(site.hits) >= 2  # resumed at the 10-minute mark
    assert (await client.get("/autowatch")).json()["state"] == "running"


async def test_hotsites_and_manual_checks_jump_the_queue(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await client.put("/settings", json={"per_host_concurrency": 1, "per_host_min_gap_s": 5.0})
    for i in range(5):
        site.set(f"/h{i}", article(f"h{i}"))
    ids = [await add(client, site.url(f"/h{i}"), priority=1 if i == 4 else 0) for i in range(5)]
    await clock.settle()
    await engine.scheduler.wait_idle()
    # the first to arrive started immediately; the hotsite is next in line, ahead of 1-3
    clock.advance(5.0)
    await clock.settle()
    await engine.scheduler.wait_idle()
    order = [h.path for h in site.hits]
    assert order[0] == "/h0" and order[1] == "/h4"
    assert (await client.post(f"/bookmarks/{ids[3]}/check")).json()["queued"] == 1  # manual: front
    clock.advance(5.0)
    await clock.settle()
    await engine.scheduler.wait_idle()
    assert [h.path for h in site.hits][2] == "/h3"


async def test_changes_inside_the_window_coalesce_into_one_summary_toast(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await client.put("/settings", json={"toast_coalesce_s": 30})
    for i in range(5):
        site.set(f"/k{i}", article(f"k{i} v1"))
        await add(client, site.url(f"/k{i}"), name=f"Site {i}")
    await settle_no_actions(engine, clock)
    for i in range(5):
        site.set(f"/k{i}", article(f"k{i} v2"))
    await advance_checks_only(engine, clock, 61, step=61)  # all five are due and detected now
    await clock.settle()
    assert not toasts.shown  # ...and wait inside the same 30 s window
    clock.advance(31)
    await clock.settle()
    await engine.actions.wait_idle()
    assert len(toasts.shown) == 1
    t = toasts.shown[0]
    assert t.title == "5 bookmarks changed" and len(t.change_ids) == 5 and not t.buttons
    # a later lone change gets its own toast with buttons
    site.set("/k0", article("k0 v3"))
    await advance_checks_only(engine, clock, 61, step=61)
    clock.advance(31)
    await clock.settle()
    await engine.actions.wait_idle()
    assert len(toasts.shown) == 2 and toasts.shown[1].title == "Site 0" and toasts.shown[1].buttons


async def settle_no_actions(engine: Engine, clock: FakeClock) -> None:
    await clock.settle()
    await engine.scheduler.wait_idle()


async def advance_checks_only(
    engine: Engine, clock: FakeClock, seconds: float, step: float = 10.0
) -> None:
    """Advance time without waiting for action jobs (toast windows legitimately span time)."""
    t = 0.0
    while t < seconds:
        clock.advance(step)
        t += step
        await settle_no_actions(engine, clock)
