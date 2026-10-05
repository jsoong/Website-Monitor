from __future__ import annotations

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle


async def test_first_check_then_change_makes_one_toast(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/news", article("Tea costs $4"))
    r = await client.post("/bookmarks", json={"url": site.url("/news"), "name": "News",
                                              "schedule": {"interval_s": 60, "jitter_pct": 0}})  # fmt: skip
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    await settle(engine, clock)

    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["status"] == "ok" and b["latest_version_id"] == b["baseline_version_id"]
    assert not toasts.shown  # the first check only stores the baseline

    await advance(engine, clock, 120)  # unchanged
    assert not toasts.shown

    site.set("/news", article("Tea costs $3"))
    await advance(engine, clock, 90)
    assert [t.title for t in toasts.shown] == ["News"]
    assert "Tea costs $3" in toasts.shown[0].body
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["status"] == "changed" and b["unread"] is True
    assert b["baseline_version_id"] != b["latest_version_id"]

    changes = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    assert len(changes) == 1 and changes[0]["added_words"] == 1 and changes[0]["removed_words"] == 1

    marked = (await client.post(f"/bookmarks/{bid}/read")).json()
    assert marked["unread"] is False and marked["status"] == "ok"
    assert marked["baseline_version_id"] == marked["latest_version_id"]
