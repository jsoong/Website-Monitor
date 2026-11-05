"""M7: virtual folders (saved searches) through the real API."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def saved(client: httpx.AsyncClient, name: str, query: dict[str, Any], **kw: Any) -> int:
    r = await client.post("/folders", json={"name": name, "is_virtual": True, "query": query, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def ids(client: httpx.AsyncClient, **params: Any) -> list[int]:
    r = await client.get("/bookmarks", params=params)
    assert r.status_code == 200, r.text
    return sorted(b["id"] for b in r.json()["items"])


# -- creation and validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"is_virtual": True, "query": {}}, "criterion"),
        ({"is_virtual": True}, "criterion"),
        ({"is_virtual": True, "query": {"nonsense": 1}}, "nonsense"),
        ({"is_virtual": True, "query": {"status": "bogus"}}, "status"),
        ({"is_virtual": True, "query": {"min_errors": 0}}, "min_errors"),
        ({"is_virtual": True, "query": {"q": "x"}, "defaults": {"priority": 1}}, "defaults"),
        ({"is_virtual": False, "query": {"q": "x"}}, "only a virtual folder"),
    ],
)
async def test_a_bad_saved_search_is_rejected(
    client: httpx.AsyncClient, body: dict[str, Any], needle: str
) -> None:
    r = await client.post("/folders", json={"name": "S", **body})
    assert r.status_code == 422 and needle in r.text, r.text
    assert (await client.get("/folders")).json() == []


async def test_a_virtual_folder_holds_no_bookmarks_and_no_folders(
    client: httpx.AsyncClient,
) -> None:
    vf = await saved(client, "Errors", {"status": "error"})
    b = await add(client, "https://example.com/a")
    r = await client.post("/bookmarks", json={"url": "https://example.com/b", "folder_id": vf})
    assert r.status_code == 422 and "saved search" in r.text
    r = await client.patch(f"/bookmarks/{b}", json={"folder_id": vf})
    assert r.status_code == 422 and "saved search" in r.text
    r = await client.post("/bookmarks/bulk", json={"ids": [b], "action": "move", "folder_id": vf})
    assert r.status_code == 422
    r = await client.post("/folders", json={"name": "Sub", "parent_id": vf})
    assert r.status_code == 422 and "cannot contain" in r.text
    plain = (await client.post("/folders", json={"name": "Plain"})).json()["id"]
    r = await client.patch(f"/folders/{plain}", json={"parent_id": vf})
    assert r.status_code == 422


async def test_the_query_is_stored_normalised_and_can_be_edited(
    client: httpx.AsyncClient,
) -> None:
    vf = await saved(client, "Mixed", {"q": "news", "status": "changed", "unread": None})
    (folder,) = (await client.get("/folders")).json()
    assert folder["is_virtual"] and folder["query"] == {"q": "news", "status": "changed"}
    r = await client.patch(f"/folders/{vf}", json={"query": {"enabled": False}})
    assert r.status_code == 200 and r.json()["query"] == {"enabled": False}
    r = await client.patch(f"/folders/{vf}", json={"query": {}})
    assert r.status_code == 422
    plain = (await client.post("/folders", json={"name": "Plain"})).json()["id"]
    r = await client.patch(f"/folders/{plain}", json={"query": {"q": "x"}})
    assert r.status_code == 422
    r = await client.patch(f"/folders/{vf}", json={"defaults": {"priority": 1}})
    assert r.status_code == 422


# -- resolving a saved search -------------------------------------------------------------------


async def test_the_list_endpoint_resolves_a_saved_search(client: httpx.AsyncClient) -> None:
    a = await add(client, "https://news.example.com/a", name="Daily news", check_method="static",
                  enabled=False)  # fmt: skip
    b = await add(client, "https://news.example.com/b", name="Weekly news")
    c = await add(client, "https://shop.example.com/c", name="Shop", source_type="feed")
    news = await saved(client, "News", {"q": "news"})
    off = await saved(client, "Disabled", {"enabled": False})
    feeds = await saved(client, "Feeds", {"source_type": "feed"})
    static = await saved(client, "Static", {"check_method": "static"})
    assert await ids(client, folder=news) == [a, b]
    assert await ids(client, folder=off) == [a]
    assert await ids(client, folder=feeds) == [c]
    assert await ids(client, folder=static) == [a]
    # the request's own filters narrow the saved search further
    assert await ids(client, folder=news, q="weekly") == [b]
    assert await ids(client, folder=news, enabled="true") == [b]
    assert await ids(client, folder=off, enabled="true") == []  # contradictory: nothing
    page = (await client.get("/bookmarks", params={"folder": news, "limit": 1})).json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["next_cursor"]
    rest = (
        await client.get("/bookmarks", params={"folder": news, "limit": 1,
                                               "cursor": page["next_cursor"]})
    ).json()  # fmt: skip
    assert [x["id"] for x in rest["items"]] == [b] and rest["next_cursor"] is None


async def test_a_saved_search_can_point_at_a_real_folder(client: httpx.AsyncClient) -> None:
    outer = (await client.post("/folders", json={"name": "Outer"})).json()["id"]
    inner = (await client.post("/folders", json={"name": "Inner", "parent_id": outer})).json()["id"]
    a = await add(client, "https://example.com/a", folder_id=outer)
    b = await add(client, "https://example.com/b", folder_id=inner)
    await add(client, "https://example.com/c")
    vf = await saved(client, "Outer tree", {"folder": outer})
    assert await ids(client, folder=vf) == [a, b]  # the folder's subfolders are included
    # ... and a real-folder filter on the request narrows to the intersection
    assert await ids(client, folder=vf, status="new") == [a, b]


async def test_error_and_change_criteria_follow_the_checks(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/ok", article("one"))
    site.set("/gone", "nope", status=404)
    ok = await add(client, site.url("/ok"), gate={"error_threshold": 1})
    bad = await add(client, site.url("/gone"), gate={"error_threshold": 1})
    unread = await saved(client, "Unread", {"unread": True})
    failing = await saved(client, "Failing", {"min_errors": 1})
    recent = await saved(client, "Changed within an hour", {"changed_within_h": 1})
    await settle(engine, clock)
    assert await ids(client, folder=failing) == [bad]
    assert await ids(client, folder=unread) == [] and await ids(client, folder=recent) == []
    site.set("/ok", article("one", "two"))
    await advance(engine, clock, 70, step=10)
    assert await ids(client, folder=unread) == [ok] == await ids(client, folder=recent)
    await advance(engine, clock, 3 * 3600, step=600)  # the change is now more than an hour old
    assert await ids(client, folder=recent) == []
    assert await ids(client, folder=unread) == [ok]  # unread has no age limit


async def test_counts_include_saved_searches_and_check_runs_them(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/a", article("a"))
    site.set("/b", article("b"))
    a = await add(client, site.url("/a"), name="Alpha", schedule={"mode": "manual"})
    b = await add(client, site.url("/b"), name="Beta", schedule={"mode": "manual"})
    await settle(engine, clock)  # every new bookmark is checked once, whatever its schedule
    vf = await saved(client, "Alphas", {"q": "alpha"})
    counts = (await client.get("/bookmarks/counts")).json()
    assert counts["by_folder"][str(vf)] == {"total": 1, "unread": 0}
    r = await client.post("/check", json={"folder_id": vf})
    assert r.json() == {"queued": 1}
    await settle(engine, clock)
    assert len((await client.get(f"/bookmarks/{a}/runs")).json()["items"]) == 2  # only Alpha
    assert len((await client.get(f"/bookmarks/{b}/runs")).json()["items"]) == 1


async def test_deleting_a_saved_search_leaves_the_bookmarks(client: httpx.AsyncClient) -> None:
    b = await add(client, "https://example.com/a")
    vf = await saved(client, "All", {"enabled": True})
    assert (await client.delete(f"/folders/{vf}")).status_code == 204
    assert await ids(client) == [b]
    assert (await client.get("/bookmarks", params={"folder": vf})).json()["total"] == 0
