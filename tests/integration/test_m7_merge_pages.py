"""M7: merge pages: a listing spread over several pages is one document to the pipeline."""

from __future__ import annotations

from typing import Any

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


def listing(*items: str, next_href: str | None = None, title: str = "Jobs") -> str:
    nxt = f'<a class="next" href="{next_href}">Next page</a>' if next_href else ""
    lis = "".join(f"<li>{i}</li>" for i in items)
    return (
        f"<html><head><title>{title}</title></head><body><h1>{title}</h1><ul>{lis}</ul>"
        f"<nav>{nxt}</nav><footer>Footer</footer></body></html>"
    )


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out


async def new_view(client: httpx.AsyncClient, bid: int) -> str:
    r = await client.get(f"/bookmarks/{bid}/diff", params={"view": "new", "format": "json"})
    return str(r.json()["html"])


def three_pages(site: FixtureSite) -> None:
    site.set("/jobs", listing("Job 1", "Job 2", next_href="/jobs/2"))
    site.set("/jobs/2", listing("Job 3", "Job 4", next_href="/jobs/3"))
    site.set("/jobs/3", listing("Job 5"))


# -- following a next-page link ---------------------------------------------------------------


async def test_pages_reached_by_a_next_link_are_one_document(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    three_pages(site)
    bid = await add(client, site.url("/jobs"), fetch={"merge": {"next_selector": "a.next"}})
    await settle(engine, clock)
    html = await new_view(client, bid)
    assert all(f"Job {n}" in html for n in range(1, 6))
    assert html.index("Job 1") < html.index("Job 3") < html.index("Job 5")  # in page order
    assert [h.path for h in site.hits] == ["/jobs", "/jobs/2", "/jobs/3"]

    # a change on the second page is detected, once, and named
    site.set("/jobs/2", listing("Job 3", "Job 4", "Job 4b", next_href="/jobs/3"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Job 4b" in toasts.shown[0].body
    # a change on the last page too
    site.set("/jobs/3", listing("Job 5", "Job 6"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 2 and "Job 6" in toasts.shown[1].body
    # the highlight view marks the new item inside the merged page
    hl = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "highlight",
                                                              "format": "json"})).json()  # fmt: skip
    assert hl["view"] == "highlight" and "pw-add" in hl["html"] and "Job 6" in hl["html"]
    assert (await runs(client, bid))[0]["outcome"] == "changed"


async def test_every_page_is_fetched_every_time_so_a_304_on_page_one_hides_nothing(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/jobs", listing("Job 1", next_href="/jobs/2"), etag=True)  # page 1 never changes
    site.set("/jobs/2", listing("Job 2"))
    await add(client, site.url("/jobs"), fetch={"merge": {"next_selector": "a.next"}})
    await settle(engine, clock)
    site.set("/jobs/2", listing("Job 2", "Job 2b"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # not hidden by a 304 on the first page
    sent = [{k.lower() for k in h.headers} for h in site.hits_for("/jobs")]
    assert all("if-none-match" not in keys for keys in sent)


async def test_the_page_limit_and_loops_stop_the_walk(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    for n in range(1, 9):
        site.set(f"/long/{n}", listing(f"Entry {n}", next_href=f"/long/{n + 1}"))
    first = await add(client, site.url("/long/1"),
                      fetch={"merge": {"next_selector": "a.next", "max_pages": 4}})  # fmt: skip
    site.set("/loop/1", listing("A", next_href="/loop/2"))
    site.set("/loop/2", listing("B", next_href="/loop/1"))  # points back at the start
    loop = await add(client, site.url("/loop/1"), fetch={"merge": {"next_selector": "a.next"}})
    await settle(engine, clock)
    html = await new_view(client, first)
    assert "Entry 4" in html and "Entry 5" not in html  # four pages in all, the first included
    assert [h.path for h in site.hits if h.path.startswith("/long")] == [
        f"/long/{n}" for n in (1, 2, 3, 4)
    ]
    html = await new_view(client, loop)
    assert "A" in html and "B" in html
    assert [h.path for h in site.hits if h.path.startswith("/loop")] == ["/loop/1", "/loop/2"]


async def test_a_failing_later_page_fails_the_check_instead_of_storing_a_partial_page(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    three_pages(site)
    bid = await add(client, site.url("/jobs"), fetch={"merge": {"next_selector": "a.next"}},
                    gate={"error_threshold": 1})  # fmt: skip
    await settle(engine, clock)
    site.set("/jobs/3", "boom", status=500)
    await advance(engine, clock, 130, step=10)
    assert (await runs(client, bid))[0]["outcome"] == "error"
    # only the "is failing" problem notice: the half-fetched listing did not look like a change
    assert [t.title.endswith("is failing") for t in toasts.shown] == [True]
    site.set("/jobs/3", listing("Job 5"))
    await advance(engine, clock, 200, step=10)
    assert len(toasts.shown) == 1  # and once the page is back there is no change to report
    assert (await runs(client, bid))[0]["outcome"] == "unchanged"


# -- following a URL pattern ------------------------------------------------------------------


async def test_a_url_pattern_walks_numbered_pages_until_one_is_missing(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/p/1", listing("P1 item"))
    site.set("/p/2", listing("P2 item"))
    site.set("/p/3", listing("P3 item"))  # /p/4 does not exist: a 404 ends the walk
    bid = await add(client, site.url("/p/1"),
                    fetch={"merge": {"url_pattern": "/p/{n}", "max_pages": 10}})  # fmt: skip
    await settle(engine, clock)
    html = await new_view(client, bid)
    assert all(f"P{n} item" in html for n in (1, 2, 3))
    assert [h.path for h in site.hits if h.path.startswith("/p/")][-1] == "/p/4"
    assert (await runs(client, bid))[0]["outcome"] == "first"  # a 404 past the end is not an error
    site.set("/p/4", listing("P4 item"))  # the listing grew a page
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "P4 item" in toasts.shown[0].body


async def test_a_url_pattern_stops_when_the_site_repeats_its_last_page(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/q/1", listing("Q1"))
    site.set("/q/2", listing("Q2"))
    for n in range(3, 8):
        site.set(f"/q/{n}", listing("Q2"))  # beyond the end it serves the last page again
    bid = await add(client, site.url("/q/1"), fetch={"merge": {"url_pattern": "/q/{n}"}})
    await settle(engine, clock)
    html = await new_view(client, bid)
    assert html.count("Q2") == 1 and "Q1" in html
    assert len([h for h in site.hits if h.path.startswith("/q/")]) == 3  # 1, 2, and the repeat


async def test_a_query_string_pattern_and_first_n(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/search", listing("Result"))  # the query string does not select a page here
    bid = await add(client, site.url("/search?page=0"),
                    fetch={"merge": {"url_pattern": "?page={n}", "first_n": 1, "max_pages": 3}})  # fmt: skip
    await settle(engine, clock)
    assert [h.query for h in site.hits] == ["page=0", "page=1"]  # page 2 repeats page 1's bytes
    assert bid


# -- configuration ----------------------------------------------------------------------------


async def test_a_merge_configuration_must_name_exactly_one_way_to_the_next_page(
    client: httpx.AsyncClient,
) -> None:
    for merge, needle in [
        ({}, "exactly one"),
        ({"next_selector": "a.next", "url_pattern": "/p/{n}"}, "exactly one"),
        ({"url_pattern": "/p/next"}, "{n}"),
        ({"next_selector": "a[["}, "invalid css selector"),
        ({"next_selector": "a.next", "max_pages": 1}, "max_pages"),
        ({"next_selector": "a.next", "max_pages": 99}, "max_pages"),
    ]:
        r = await client.post(
            "/bookmarks", json={"url": "https://example.com/", "fetch": {"merge": merge}}
        )
        assert r.status_code == 422 and needle in r.text, (merge, r.text)
    ok = await client.post(
        "/bookmarks",
        json={"url": "https://example.com/", "fetch": {"merge": {"enabled": False}}},
    )
    assert ok.status_code == 201  # disabled: nothing to validate
