from __future__ import annotations

from typing import Any

import httpx
import pytest

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import settle

URL = "https://example.com/page"


async def mk(client: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {"url": URL, "schedule": {"mode": "manual"}, **kw}
    r = await client.post("/bookmarks", json=body)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


# -- create / validation ----------------------------------------------------------------


async def test_create_defaults_and_effective_config(client: httpx.AsyncClient) -> None:
    b = await mk(client)
    assert b["name"] == "example.com" and b["status"] == "new" and b["enabled"] is True
    assert b["actions"]["actions"] == [{"type": "toast", "params": {}}]  # toast on by default
    assert b["actions"]["alert_privacy"] == "content"
    assert (
        b["gate"]["threshold_mode"] == "cumulative"
        and b["filter"]["special"]["ignore_case"] is True
    )
    assert b["overrides"]["schedule"] == {"mode": "manual"}  # only what the user set is stored
    assert b["fetch"]["timeout_s"] == 30.0 and b["source_type"] == "auto"


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"url": "javascript:alert(1)"}, "url"),
        ({"schedule": {"interval_s": 30}}, "interval_s"),
        ({"schedule": {"mode": "times"}}, "times"),
        ({"gate": {"nonsense": 1}}, "nonsense"),
        (
            {"filter": {"ignore": [{"type": "text", "pattern": "(", "pattern_kind": "regex"}]}},
            "regex",
        ),
        ({"priority": 5}, "priority"),
        ({"folder_id": 999}, "999"),
    ],
)
async def test_invalid_input_is_rejected_with_a_useful_message(
    client: httpx.AsyncClient, patch: dict[str, Any], needle: str
) -> None:
    r = await client.post("/bookmarks", json={"url": URL, **patch})
    assert r.status_code == 422, r.text
    assert needle in r.text
    assert (await client.get("/bookmarks")).json()["total"] == 0  # nothing half-created


async def test_bookmark_not_found(client: httpx.AsyncClient) -> None:
    for call in (client.get("/bookmarks/99"), client.delete("/bookmarks/99"),
                 client.post("/bookmarks/99/check"), client.post("/bookmarks/99/read"),
                 client.patch("/bookmarks/99", json={"name": "x"})):  # fmt: skip
        assert (await call).status_code == 404


# -- patch semantics --------------------------------------------------------------------


async def test_patch_merges_sparse_overrides_and_null_restores_inheritance(
    client: httpx.AsyncClient,
) -> None:
    b = await mk(client, schedule={"interval_s": 600, "jitter_pct": 0})
    r = await client.patch(
        f"/bookmarks/{b['id']}", json={"schedule": {"jitter_pct": 5}, "note": "hi"}
    )
    assert r.status_code == 200
    out = r.json()
    assert (
        out["overrides"]["schedule"] == {"interval_s": 600, "jitter_pct": 5} and out["note"] == "hi"
    )
    out = (
        await client.patch(f"/bookmarks/{b['id']}", json={"schedule": {"jitter_pct": None}})
    ).json()
    assert out["overrides"]["schedule"] == {"interval_s": 600}
    assert out["schedule"]["jitter_pct"] == 10  # back to the default
    # an invalid patch changes nothing
    bad = await client.patch(f"/bookmarks/{b['id']}", json={"schedule": {"interval_s": 1}})
    assert bad.status_code == 422
    assert (await client.get(f"/bookmarks/{b['id']}")).json()["overrides"]["schedule"] == {
        "interval_s": 600
    }


async def test_changing_url_resets_versions_and_disable_enable_cycle(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/a", article("one")), site.set("/b", article("two"))
    b = await mk(client, url=site.url("/a"), schedule={"mode": "manual"})
    await settle(engine, clock)
    assert (await client.get(f"/bookmarks/{b['id']}")).json()["latest_version_id"] is not None

    r = (await client.patch(f"/bookmarks/{b['id']}", json={"url": site.url("/b")})).json()
    assert r["latest_version_id"] is None and r["status"] == "new"  # a different page: new baseline
    await settle(engine, clock)
    assert (await client.get(f"/bookmarks/{b['id']}")).json()["status"] == "ok"

    off = (await client.patch(f"/bookmarks/{b['id']}", json={"enabled": False})).json()
    assert off["status"] == "disabled"
    hits = len(site.hits)
    assert (await client.post(f"/bookmarks/{b['id']}/check")).json()[
        "queued"
    ] == 1  # manual still works
    await settle(engine, clock)
    assert len(site.hits) == hits + 1
    on = (await client.patch(f"/bookmarks/{b['id']}", json={"enabled": True})).json()
    assert on["enabled"] is True and on["status"] == "ok"


# -- folders and inheritance ------------------------------------------------------------


async def test_folder_defaults_are_inherited_overridden_and_validated(
    client: httpx.AsyncClient,
) -> None:
    root = (await client.post("/folders", json={"name": "News", "defaults": {
        "schedule": {"interval_s": 900, "jitter_pct": 0}, "gate": {"min_chars": 40},
        "check_method": "static", "priority": 1,
    }})).json()  # fmt: skip
    child = (await client.post("/folders", json={"name": "Local", "parent_id": root["id"],
                                                 "defaults": {"gate": {"min_changed_words": 3}}})).json()  # fmt: skip
    a = await mk(client, folder_id=child["id"], schedule=None)
    assert a["schedule"]["interval_s"] == 900  # from the grandparent
    assert (
        a["gate"]["min_chars"] == 40 and a["gate"]["min_changed_words"] == 3
    )  # merged down the chain
    assert (
        a["check_method"] == "static" and a["priority"] == 1
    )  # scalar defaults copied at creation
    own = await mk(client, folder_id=child["id"], schedule={"interval_s": 120}, check_method="auto")
    assert own["schedule"]["interval_s"] == 120 and own["check_method"] == "auto"
    # editing the folder changes what unmodified bookmarks inherit
    await client.patch(
        f"/folders/{root['id']}", json={"defaults": {"schedule": {"interval_s": 1800}}}
    )
    assert (await client.get(f"/bookmarks/{a['id']}")).json()["schedule"]["interval_s"] == 1800
    assert (await client.get(f"/bookmarks/{own['id']}")).json()["schedule"]["interval_s"] == 120
    # invalid defaults are refused
    bad = await client.patch(
        f"/folders/{root['id']}", json={"defaults": {"schedule": {"interval_s": 5}}}
    )
    assert bad.status_code == 422
    assert (
        await client.post("/folders", json={"name": "x", "defaults": {"bogus": 1}})
    ).status_code == 422


async def test_folder_cycle_guard_and_delete(client: httpx.AsyncClient) -> None:
    a = (await client.post("/folders", json={"name": "A"})).json()
    b = (await client.post("/folders", json={"name": "B", "parent_id": a["id"]})).json()
    assert (
        await client.patch(f"/folders/{a['id']}", json={"parent_id": b["id"]})
    ).status_code == 422
    assert (
        await client.patch(f"/folders/{a['id']}", json={"parent_id": a["id"]})
    ).status_code == 422
    bm = await mk(client, folder_id=b["id"])
    assert (await client.delete(f"/folders/{a['id']}")).status_code == 204  # B goes with it
    assert [f["name"] for f in (await client.get("/folders")).json()] == []
    got = (await client.get(f"/bookmarks/{bm['id']}")).json()
    assert got["folder_id"] is None  # bookmarks survive, back at the root
    assert (await client.delete(f"/folders/{a['id']}")).status_code == 404


async def test_move_between_folders_and_to_root(client: httpx.AsyncClient) -> None:
    f = (await client.post("/folders", json={"name": "F"})).json()
    b = await mk(client)
    assert (await client.patch(f"/bookmarks/{b['id']}", json={"folder_id": f["id"]})).json()[
        "folder_id"
    ] == f["id"]
    assert (await client.patch(f"/bookmarks/{b['id']}", json={"move_to_root": True})).json()[
        "folder_id"
    ] is None


# -- listing ----------------------------------------------------------------------------


async def test_list_filters_sorting_and_keyset_paging(client: httpx.AsyncClient) -> None:
    f = (await client.post("/folders", json={"name": "F"})).json()
    sub = (await client.post("/folders", json={"name": "Sub", "parent_id": f["id"]})).json()
    for i in range(12):
        await mk(client, name=f"item {i:02d}", url=f"https://e{i % 3}.com/{i}",
                 folder_id=sub["id"] if i < 5 else None)  # fmt: skip
    seen: list[int] = []
    cursor = None
    while True:
        page = (await client.get("/bookmarks", params={"limit": 5, "sort": "name", "desc": True,
                                                         **({"cursor": cursor} if cursor else {})})).json()  # fmt: skip
        seen += [x["id"] for x in page["items"]]
        assert page["total"] == 12
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 12
    names = [
        x["name"]
        for x in (await client.get("/bookmarks", params={"sort": "name", "limit": 100})).json()[
            "items"
        ]
    ]
    assert names == sorted(names)
    assert (await client.get("/bookmarks", params={"folder": f["id"]})).json()[
        "total"
    ] == 5  # subfolders included
    assert (await client.get("/bookmarks", params={"folder": f["id"], "subfolders": False})).json()[
        "total"
    ] == 0
    assert (await client.get("/bookmarks", params={"q": "e1.com"})).json()["total"] == 4
    assert (await client.get("/bookmarks", params={"q": "100%"})).json()[
        "total"
    ] == 0  # LIKE wildcards escaped
    assert (await client.get("/bookmarks", params={"status": "new"})).json()["total"] == 12
    assert (await client.get("/bookmarks", params={"unread": True})).json()["total"] == 0
    assert (await client.get("/bookmarks", params={"cursor": "garbage"})).status_code == 400
    assert (await client.get("/bookmarks", params={"sort": "nope"})).status_code == 400
    assert (await client.get("/bookmarks", params={"limit": 0})).status_code == 422


async def test_bulk_actions(client: httpx.AsyncClient) -> None:
    f = (await client.post("/folders", json={"name": "F"})).json()
    ids = [(await mk(client, name=f"b{i}"))["id"] for i in range(4)]
    r = await client.post(
        "/bookmarks/bulk", json={"ids": ids, "action": "move", "folder_id": f["id"]}
    )
    assert r.json() == {"affected": 4}
    r = await client.post("/bookmarks/bulk", json={"ids": ids[:2], "action": "update",
                                                   "patch": {"gate": {"min_chars": 77}, "note": "bulk"}})  # fmt: skip
    assert r.json()["affected"] == 2
    assert (await client.get(f"/bookmarks/{ids[0]}")).json()["gate"]["min_chars"] == 77
    assert (await client.get(f"/bookmarks/{ids[3]}")).json()["gate"]["min_chars"] == 0
    await client.post("/bookmarks/bulk", json={"ids": ids[:3], "action": "disable"})
    assert (await client.get("/bookmarks", params={"enabled": False})).json()["total"] == 3
    r = await client.post("/bookmarks/bulk", json={"ids": [*ids, 999], "action": "delete"})
    assert r.json()["affected"] == 4
    assert (await client.get("/bookmarks")).json()["total"] == 0
    assert (
        await client.post("/bookmarks/bulk", json={"ids": ids, "action": "update"})
    ).status_code == 422


# -- settings and autowatch -------------------------------------------------------------


async def test_settings_partial_update_validation_and_persistence(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    s = (await client.get("/settings")).json()
    assert s["static_pool"] == 32 and s["browser_pool"] == 3
    out = (
        await client.put(
            "/settings", json={"keep_changed_versions": 7, "timezone": "America/New_York"}
        )
    ).json()
    assert out["keep_changed_versions"] == 7 and out["static_pool"] == 32
    assert (await client.put("/settings", json={"timezone": "Not/AZone"})).status_code == 422
    assert (await client.put("/settings", json={"static_pool": 0})).status_code == 422
    assert (await client.put("/settings", json={"nope": 1})).status_code == 422
    rows = await engine.db.read(
        lambda c: {r["key"]: r["value_json"] for r in c.execute("SELECT * FROM setting")}
    )
    assert rows == {
        "keep_changed_versions": "7",
        "timezone": '"America/New_York"',
    }  # only what changed
