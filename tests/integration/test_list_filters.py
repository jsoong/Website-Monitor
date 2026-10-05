from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from pagewatch.engine.clock import FakeClock, iso
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    r = await client.post(
        "/bookmarks", json={"url": url, "schedule": {"interval_s": 60, "jitter_pct": 0}, **kw}
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def test_counts_changed_since_and_keyword_hits_drive_the_builtin_folders(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    folder = (await client.post("/folders", json={"name": "News"})).json()["id"]
    for name, kw in (
        ("quiet", {}),
        ("hits", {"gate": {"keywords": "breaking"}}),
        ("plain", {"folder_id": folder}),
    ):
        site.set(f"/{name}", article("calm", title=name))
        await add(client, site.url(f"/{name}"), name=name, **kw)
    broken = await add(client, site.url("/missing"), name="broken", gate={"error_threshold": 1})
    await settle(engine, clock)
    site.set("/hits", article("calm", "breaking news today", title="hits"))
    site.set("/plain", article("calm", "something else", title="plain"))
    await advance(engine, clock, 70, step=10)

    counts = (await client.get("/bookmarks/counts")).json()  # not shadowed by /bookmarks/{id}
    assert counts["total"] == 4 and counts["unread"] == 2 and counts["errors"] == 1
    assert (
        counts["changed_today"] == 2 and counts["keyword_hits"] == 1 and counts["needs_login"] == 0
    )
    assert counts["by_folder"][str(folder)] == {"total": 1, "unread": 1}
    assert counts["by_folder"]["0"]["total"] == 3

    hits = (await client.get("/bookmarks", params={"keyword_hits": True})).json()
    assert [b["name"] for b in hits["items"]] == ["hits"] and hits["items"][0]["keyword_hits"] == [
        "breaking"
    ]
    since = iso(clock.now() - timedelta(hours=24))
    today = (await client.get("/bookmarks", params={"changed_since": since})).json()
    assert sorted(b["name"] for b in today["items"]) == ["hits", "plain"]
    future = iso(clock.now() + timedelta(hours=1))
    assert (await client.get("/bookmarks", params={"changed_since": future})).json()["total"] == 0
    errors = (await client.get("/bookmarks", params={"status": "error"})).json()
    assert [b["id"] for b in errors["items"]] == [broken]
    # reading clears the unread keyword hits from the summary and the filter
    await client.post(f"/bookmarks/{(hits['items'][0]['id'])}/read")
    assert (await client.get("/bookmarks", params={"keyword_hits": True})).json()["total"] == 0
    assert (await client.get("/bookmarks/counts")).json()["keyword_hits"] == 0
