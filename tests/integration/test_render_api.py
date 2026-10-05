from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    r = await client.post(
        "/bookmarks", json={"url": url, "schedule": {"interval_s": 60, "jitter_pct": 0}, **kw}
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def cache_rows(engine: Engine) -> list[sqlite3.Row]:
    return engine.db.read_sync(lambda c: c.execute("SELECT * FROM view_diff_cache").fetchall())


async def two_changes(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> int:
    site.set("/p", article("alpha one", "beta two", title="News"))
    bid = await add(client, site.url("/p"), name="News")
    await settle(engine, clock)
    site.set("/p", article("alpha ONE changed", "beta two", title="News"))
    await advance(engine, clock, 70, step=10)
    site.set("/p", article("alpha ONE changed", "beta two", "gamma three", title="News"))
    await advance(engine, clock, 70, step=10)
    return bid


async def test_unread_diff_accumulates_while_history_shows_each_alert(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await two_changes(client, engine, clock, site)
    changes = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    assert len(changes) == 2

    # the viewer's default: last read -> latest, so BOTH edits are visible together
    r = await client.get(f"/bookmarks/{bid}/diff", params={"view": "highlight"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert (
        r.headers["x-pagewatch-identical"] == "false"
        and r.headers["x-pagewatch-view"] == "highlight"
    )
    assert "<ins" in r.text and "gamma three" in r.text and "changed" in r.text
    assert "Content-Security-Policy" in r.text and "<script" not in r.text

    # history: each alert's own gate diff
    newest, oldest = changes[0], changes[1]
    h_new = (await client.get(f"/changes/{newest['id']}/render", params={"view": "highlight"})).text
    h_old = (await client.get(f"/changes/{oldest['id']}/render", params={"view": "highlight"})).text
    assert "gamma three" in h_new and "ONE" not in h_new.split("<body")[1].split("</body>")[
        0
    ].replace("ONE changed", "")
    assert "ONE" in h_old and "gamma" not in h_old.split("<body")[1].split("</body>")[0]


async def test_text_view_old_new_and_json_format(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await two_changes(client, engine, clock, site)
    text = await client.get(f"/bookmarks/{bid}/diff", params={"view": "text"})
    assert 'class="pw-b pw-ins"' in text.text and 'class="pw-b pw-rep"' in text.text
    new = await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})
    old = await client.get(f"/bookmarks/{bid}/diff", params={"view": "old"})
    assert "gamma three" in new.text and "gamma three" not in old.text and "alpha one" in old.text
    assert "<ins" not in new.text.split("<body")[1] and "<ins" not in old.text.split("<body")[1]
    j = (
        await client.get(f"/bookmarks/{bid}/diff", params={"view": "highlight", "format": "json"})
    ).json()
    assert (
        set(j) == {"html", "view", "identical", "degraded", "stats"}
        and j["stats"]["added_words"]
        == 3  # "changed" + "gamma three"; one -> ONE is only a case change
    )
    # change-level views, including the unmarked pages and the missing screenshot
    cid = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"][0]["id"]
    for view in ("new", "old", "text", "highlight"):
        assert (
            await client.get(f"/changes/{cid}/render", params={"view": view})
        ).status_code == 200
    assert (
        await client.get(f"/changes/{cid}/render", params={"view": "screenshot"})
    ).status_code == 404
    assert (await client.get(f"/changes/{cid}/render", params={"view": "nope"})).status_code == 422
    assert (await client.get("/changes/999/render")).status_code == 404
    assert (await client.get("/bookmarks/999/diff")).status_code == 404


async def test_the_unread_diff_is_cached_one_row_per_bookmark_and_replaced_when_a_pointer_moves(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/p", article("alpha", title="N"))
    bid = await add(client, site.url("/p"))
    await settle(engine, clock)
    site.set("/p", article("alpha", "beta", title="N"))
    await advance(engine, clock, 70, step=10)
    assert cache_rows(engine) == []

    await client.get(f"/bookmarks/{bid}/diff")
    (row1,) = cache_rows(engine)
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert (row1["baseline_version_id"], row1["latest_version_id"]) == (
        b["baseline_version_id"],
        b["latest_version_id"],
    )

    calls = {"n": 0}
    from pagewatch.engine import changes as ops

    real = ops.compute_view_diff

    def counting(job: Any) -> Any:
        calls["n"] += 1
        return real(job)

    ops.compute_view_diff = counting  # type: ignore[assignment]
    try:
        await client.get(f"/bookmarks/{bid}/diff")  # served from the cache
        assert calls["n"] == 0
        site.set("/p", article("alpha", "beta", "gamma", title="N"))
        await advance(engine, clock, 70, step=10)
        assert cache_rows(engine) == []  # the new version invalidated the row in the same commit
        await client.get(f"/bookmarks/{bid}/diff")
        assert calls["n"] == 1
        (row2,) = cache_rows(engine)
        assert row2["latest_version_id"] != row1["latest_version_id"]
    finally:
        ops.compute_view_diff = real  # type: ignore[assignment]

    # mark read: nothing unread, the cache row goes, and the viewer says so
    await client.post(f"/bookmarks/{bid}/read")
    assert cache_rows(engine) == []
    r = await client.get(f"/bookmarks/{bid}/diff")
    assert r.headers["x-pagewatch-identical"] == "true" and "No unread changes" in r.text


async def test_diff_before_the_first_check_is_a_conflict(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await add(client, "http://127.0.0.1:9/never", schedule={"mode": "manual"})
    await settle(engine, clock)
    assert (await client.get(f"/bookmarks/{bid}/diff")).status_code == 409


async def test_page_with_hostile_markup_is_sanitised_in_every_view(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    evil = (
        '<html><body><h1>Title</h1><p onclick="x()">Price $4</p><script>alert(1)</script>'
        "<img src='https://tracker.test/p.png' onerror='x()'><iframe src='https://e.test'></iframe></body></html>"
    )
    site.set("/e", evil)
    bid = await add(client, site.url("/e"))
    await settle(engine, clock)
    site.set("/e", evil.replace("$4", "$3"))
    await advance(engine, clock, 70, step=10)
    for view in ("highlight", "text", "new", "old"):
        body = (await client.get(f"/bookmarks/{bid}/diff", params={"view": view})).text.split(
            "<body"
        )[1]
        for bad in ("onclick", "<script", "onerror", "<iframe", "tracker.test"):
            assert bad not in body, (view, bad)
    on = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new", "images": True})).text
    assert "tracker.test" in on and "img-src data: http: https:" in on


# -- preview ------------------------------------------------------------------------------


async def test_preview_fetches_twice_and_proposes_filters_for_what_already_differs(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set_dynamic(
        "/clock",
        lambda n: (
            f"<html><body><h1>Status</h1><p>Updated {n * 5} minutes ago</p><p>All systems normal</p></body></html>"
        ),
    )
    before = (
        len(site.hits),
        engine.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0]),
    )
    import asyncio

    task = asyncio.create_task(
        client.post("/preview", json={"url": site.url("/clock"), "samples": 2, "gap_s": 5})
    )
    await asyncio.sleep(0.3)
    clock.advance(5)  # the assistant waits between fetches on the engine clock
    r = await task
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "page" and out["method"] == "static" and out["js_app"] is False
    assert out["blocks"] == 3 and out["words"] >= 7 and out["status"] == 200
    assert out["unstable_blocks"] == 1
    assert [p["pattern_name"] for p in out["proposals"]] == ["relative_time"] and out["proposals"][
        0
    ]["verified"]
    assert "All systems normal" in out["html"] and "<script" not in out["html"]
    assert len(site.hits) == before[0] + 2
    assert (
        engine.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0])
        == before[1]
    )  # nothing stored
    assert (
        engine.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM version").fetchone()[0]) == 0
    )


async def test_preview_detects_a_javascript_app_a_feed_and_failures(
    client: httpx.AsyncClient, site: FixtureSite
) -> None:
    site.set(
        "/app",
        "<html><head><script src='/main.js'></script></head><body><div id='root'></div></body></html>",
    )
    out = (await client.post("/preview", json={"url": site.url("/app")})).json()
    assert (
        out["kind"] == "js-app"
        and out["js_app"]
        and out["method"] == "browser"
        and out["readable_chars"] == 0
    )
    site.set(
        "/feed",
        "<?xml version='1.0'?><rss version='2.0'><channel><title>x</title></channel></rss>",
        content_type="application/rss+xml",
    )
    assert (await client.post("/preview", json={"url": site.url("/feed")})).json()["kind"] == "feed"
    bad = (await client.post("/preview", json={"url": site.url("/missing")})).json()
    assert bad["error"].startswith("http_404") and bad["kind"] == "error"
    assert (await client.post("/preview", json={"url": "nonsense"})).status_code == 422
    assert (
        await client.post("/preview", json={"url": site.url("/app"), "fetch": {"timeout_s": 0}})
    ).status_code == 422
