"""M7 acceptance: each shipped plugin example passes its integration test.

The examples are installed through the API exactly as a user would, assigned to a bookmark, and
driven through real checks.
"""

from __future__ import annotations

from typing import Any

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

HOURLY = {"interval_s": 3600, "jitter_pct": 0}
MINUTELY = {"interval_s": 60, "jitter_pct": 0}


async def install(client: httpx.AsyncClient, name: str) -> None:
    r = await client.post("/plugins/install", json={"name": name})
    assert r.status_code == 201, r.text


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out


async def changes(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    return list(reversed(out))


def shop(*items: str) -> str:
    return article(*items, title="Shop")


# -- price_range ------------------------------------------------------------------------------


async def test_price_range_alerts_only_for_prices_inside_the_range(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "price_range")
    site.set("/shop", shop("Widget $120.00"))
    bid = await add(client, site.url("/shop"), plugin="price_range", info1="min=100 max=250",
                    schedule=MINUTELY)  # fmt: skip
    await settle(engine, clock)

    site.set("/shop", shop("Widget $120.00", "Yacht $499,000"))
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown and (await runs(client, bid))[0]["reason"] == "keyword_miss"

    site.set("/shop", shop("Widget $120.00", "Yacht $499,000", "Gizmo $199.99", "Year 2026"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Gizmo" in toasts.shown[0].body
    (change,) = await changes(client, bid)
    assert change["keyword_hits"] == ["plugin:price_range"]

    site.set("/shop", shop("Widget $120.00", "Yacht $499,000", "Gizmo $199.99", "Year 2026",
                           "Cheap $5"))  # fmt: skip
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # $5 is below the range


async def test_price_range_is_one_sided_when_only_one_bound_is_given_and_works_with_keywords(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "price_range")
    site.set("/a", shop("Base $10"))
    site.set("/b", shop("Base $10"))
    await add(client, site.url("/a"), plugin="price_range", info1="max=1,200", schedule=MINUTELY)
    b = await add(client, site.url("/b"), plugin="price_range", info1="min=5000",
                  gate={"keywords": "clearance"}, schedule=MINUTELY)  # fmt: skip
    await settle(engine, clock)
    site.set("/a", shop("Base $10", "Laptop $1,099.00"))
    site.set("/b", shop("Base $10", "Clearance sale item"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 2  # /a: 1099 <= 1200; /b: its own keyword, the plugin says no
    (change,) = await changes(client, b)
    assert change["keyword_hits"] == ["clearance"]


async def test_price_range_without_a_range_defers_to_the_built_in_rules(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "price_range")
    site.set("/shop", shop("Widget $120"))
    await add(client, site.url("/shop"), plugin="price_range", schedule=MINUTELY)
    await settle(engine, clock)
    site.set("/shop", shop("Widget $120", "Anything at all"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # no range, no keywords: an ordinary change alerts


# -- stale_page -------------------------------------------------------------------------------


async def test_stale_page_alerts_once_when_nothing_changed_for_n_days(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "stale_page")
    site.set("/blog", article("Post one", title="Blog"))
    bid = await add(client, site.url("/blog"), plugin="stale_page", info1="days=2",
                    name="The blog", schedule=HOURLY)  # fmt: skip

    def stale() -> list[str]:
        return [t.title for t in toasts.shown if "no change" in t.title]

    await settle(engine, clock)
    await advance(engine, clock, 20 * 3600, step=3600)
    assert stale() == []
    await advance(engine, clock, 30 * 3600, step=3600)  # 50 hours after the first check
    assert stale() == ["The blog: no change for 2 days"]
    await advance(engine, clock, 4 * 86400, step=3600 * 6)
    assert len(stale()) == 1  # once per quiet period, not on every check

    site.set("/blog", article("Post one", "Post two", title="Blog"))  # it moved: start over
    await advance(engine, clock, 2 * 3600, step=3600)
    assert len([t for t in toasts.shown if t.title != "The blog: no change for 2 days"]) == 1
    await advance(engine, clock, 36 * 3600, step=3600)
    assert len(stale()) == 1
    await advance(engine, clock, 20 * 3600, step=3600)
    assert len(stale()) == 2  # quiet for two days again
    assert (await runs(client, bid))[0]["outcome"] == "unchanged"


async def test_stale_page_ignores_failing_checks(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "stale_page")
    site.set("/blog", article("Post one", title="Blog"))
    await add(client, site.url("/blog"), plugin="stale_page", info1="days=1", schedule=HOURLY,
              gate={"error_threshold": 99})  # fmt: skip
    await settle(engine, clock)
    site.set("/blog", "down", status=503)
    await advance(engine, clock, 3 * 86400, step=3600 * 4)
    assert [t for t in toasts.shown if "no change" in t.title] == []  # an outage is not staleness


# -- single_new_item --------------------------------------------------------------------------


async def test_single_new_item_stays_quiet_until_a_batch_arrives(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "single_new_item")
    site.set("/jobs", article("Job A", "Job B", title="Jobs"))
    bid = await add(client, site.url("/jobs"), plugin="single_new_item", schedule=MINUTELY)
    await settle(engine, clock)

    site.set("/jobs", article("Job A", "Job B", "Job C", title="Jobs"))
    await advance(engine, clock, 70, step=10)
    latest = (await runs(client, bid))[0]
    assert not toasts.shown
    assert (latest["outcome"], latest["reason"]) == ("suppressed", "1 new item(s), need 2")

    site.set("/jobs", article("Job A", "Job B", "Job C", "Job D", "Job E", title="Jobs"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Job D" in toasts.shown[0].body
    assert len(await changes(client, bid)) == 1

    site.set("/jobs", article("Job B", "Job C", "Job D", "Job E", title="Jobs"))  # only removed
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1


async def test_single_new_item_threshold_is_configurable_and_the_page_is_always_stored(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "single_new_item")
    site.set("/jobs", article("A", title="Jobs"))
    await add(client, site.url("/jobs"), plugin="single_new_item", info1="min_new=3",
                    schedule=MINUTELY)  # fmt: skip
    await settle(engine, clock)
    site.set("/jobs", article("A", "B", "C", title="Jobs"))  # two new: not enough for min_new=3
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown
    site.set("/jobs", article("A", "B", "C", "D", "E", "F", title="Jobs"))  # three new
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1
    await advance(engine, clock, 300, step=30)
    assert len(toasts.shown) == 1  # the suppressed versions were stored: nothing re-fires


# -- pdf_links --------------------------------------------------------------------------------


def docs_page(news: str, *pdfs: tuple[str, str]) -> str:
    links = "".join(f'<li><a href="{href}">{text}</a></li>' for text, href in pdfs)
    return (
        f"<html><body><h1>Documents</h1><p>{news}</p><ul>{links}</ul>"
        '<p><a href="/about">About us</a> <a href="/files/readme.txt">notes</a></p>'
        "</body></html>"
    )


async def test_pdf_links_watches_only_the_list_of_pdf_files(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "pdf_links")
    q1 = ("Q1 report", "/files/q1.pdf")
    site.set("/docs", docs_page("Welcome", q1))
    bid = await add(client, site.url("/docs"), plugin="pdf_links", schedule=MINUTELY)
    await settle(engine, clock)

    site.set("/docs", docs_page("Totally different news today", q1))  # prose changed: no change
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown and (await runs(client, bid))[0]["outcome"] == "unchanged"

    q2 = ("Q2 report", "/files/q2.PDF?download=1")
    site.set("/docs", docs_page("Welcome", q1, q2))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Q2 report" in toasts.shown[0].body
    assert "q2.PDF" in toasts.shown[0].body and "Q1 report" not in toasts.shown[0].body

    site.set(
        "/docs", docs_page("Welcome", q1, ("Q2 report", "/files/q2-final.pdf"))
    )  # swapped file
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 2 and "q2-final.pdf" in toasts.shown[1].body

    new = (
        await client.get(f"/bookmarks/{bid}/diff", params={"view": "new", "format": "json"})
    ).json()
    assert "Q1 report" in new["html"] and "About us" not in new["html"]  # the list only
    text = (
        await client.get(f"/bookmarks/{bid}/diff", params={"view": "text", "format": "json"})
    ).json()
    assert "q2-final.pdf" in text["html"]


async def test_pdf_links_with_no_pdfs_is_an_empty_page_and_a_bookmark_in_a_folder_keeps_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await install(client, "pdf_links")
    site.set("/docs", docs_page("Nothing yet"))
    await add(client, site.url("/docs"), plugin="pdf_links", schedule=MINUTELY)
    await settle(engine, clock)
    site.set("/docs", docs_page("Nothing yet", ("Plan", "/plan.pdf")))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Plan" in toasts.shown[0].body
