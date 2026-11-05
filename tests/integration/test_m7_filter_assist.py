"""M7: the filter assistant's engine side: DOM paths in the rendered views, the five choices, their
markers and selectors, and applying them to real checks."""

from __future__ import annotations

import re
from typing import Any

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


def digest(minutes: int = 5, visitors: str = "1,234", extra: str = "") -> str:
    return f"""<html><head><title>Digest</title></head><body>
<header><h1>Daily Digest</h1></header>
<nav>Home | News</nav>
<div id="content">
  <p class="stamp">Updated {minutes} minutes ago</p>
  <p>Welcome to the digest</p>
  <ul><li>Story one</li><li class="promo">Sponsored: buy now</li><li>Story two</li></ul>
  <p>Price: $5 today</p><p>Price: $5 tomorrow</p>
  <p>Story one</p>{extra}
</div>
<footer>Visitors today: {visitors}</footer>
</body></html>"""


async def add(client: httpx.AsyncClient, site: FixtureSite, page: str = "/d", **kw: Any) -> int:
    r = await client.post("/bookmarks", json={"url": site.url(page), "schedule": SCHED, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def view(client: httpx.AsyncClient, bid: int, which: str = "new", **params: Any) -> str:
    q = {"view": which, "format": "json", **params}
    r = await client.get(f"/bookmarks/{bid}/diff", params=q)
    assert r.status_code == 200, r.text
    return str(r.json()["html"])


async def assist(client: httpx.AsyncClient, bid: int, **body: Any) -> dict[str, Any]:
    r = await client.post(f"/bookmarks/{bid}/filter-assist", json=body)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def choice(out: dict[str, Any], id_: str) -> dict[str, Any]:
    (found,) = [c for c in out["choices"] if c["id"] == id_]
    return found


async def blocks_of(client: httpx.AsyncClient, bid: int) -> list[str]:
    r = await client.post(f"/bookmarks/{bid}/test-filter", json={})
    assert r.status_code == 200, r.text
    out: list[str] = r.json()["latest"]
    return out


async def setup(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite, **kw: Any
) -> int:
    site.set("/d", digest())
    bid = await add(client, site, **kw)
    await settle(engine, clock)
    return bid


PATHS = {
    "stamp": "/html[1]/body[1]/div[1]/p[1]",
    "promo": "/html[1]/body[1]/div[1]/ul[1]/li[2]",
    "story_two": "/html[1]/body[1]/div[1]/ul[1]/li[3]",
    "welcome": "/html[1]/body[1]/div[1]/p[2]",
    "price1": "/html[1]/body[1]/div[1]/p[3]",
    "last_story": "/html[1]/body[1]/div[1]/p[5]",
}


# -- DOM paths in the rendered views ---------------------------------------------------------


async def test_views_can_tag_elements_with_the_extractors_dom_paths(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    plain = await view(client, bid)
    assert "data-pw-path" not in plain  # off unless asked: the default output is unchanged
    tagged = await view(client, bid, paths="true")
    for path in PATHS.values():
        assert f'data-pw-path="{path}"' in tagged
    assert re.search(r'<li class="promo" data-pw-path="[^"]+li\[2\]">Sponsored', tagged)

    # the highlighted view injects <ins>/<del>, which must not shift a later element's path
    site.set("/d", digest(extra="<p>Brand new paragraph</p>").replace("Story two", "Story 2"))
    await advance(engine, clock, 70, step=10)
    hl = await view(client, bid, "highlight", paths="true")
    assert "pw-add" in hl or "pw-ins-block" in hl
    assert f'data-pw-path="{PATHS["promo"]}"' in hl and f'data-pw-path="{PATHS["welcome"]}"' in hl
    # and every path the view reports is one the assistant can find again
    for p in re.findall(r'data-pw-path="([^"]+)"', hl):
        if "/p[" in p or "/li[" in p:
            r = await client.post(f"/bookmarks/{bid}/filter-assist", json={"path": p, "text": "x"})
            assert r.status_code == 200, (p, r.text)
            assert r.json()["element"]["path"] == p


async def test_a_pages_own_data_attributes_cannot_forge_a_path(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/d", '<html><body><p data-pw-path="/html[1]/forged[9]">hello</p></body></html>')
    bid = await add(client, site)
    await settle(engine, clock)
    tagged = await view(client, bid, paths="true")
    assert "forged" not in tagged and 'data-pw-path="/html[1]/body[1]/p[1]"' in tagged


# -- the five choices ---------------------------------------------------------------------------


async def test_the_five_choices_for_a_selection(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    out = await assist(client, bid, path=PATHS["stamp"], text="Updated 5 minutes ago")
    assert [c["id"] for c in out["choices"]] == [
        "ignore_element", "watch_element", "ignore_text", "ignore_to_here", "ignore_from_here",
    ]  # fmt: skip
    assert out["selection"] == "Updated 5 minutes ago" and out["block_text"] == out["selection"]
    el = out["element"]
    assert (el["tag"], el["selector"], el["selector_kind"]) == ("p", "p.stamp", "css")
    assert el["can_widen"] and el["levels_up"] == 0

    ignore, watch = choice(out, "ignore_element"), choice(out, "watch_element")
    assert ignore["section"] == "ignore" and watch["section"] == "watch"
    assert ignore["rule"]["type"] == "selector" and ignore["rule"]["selector"] == "p.stamp"
    assert ignore["patch"] == {
        "filter": {
            "ignore": [
                {
                    "type": "selector",
                    "selector": "p.stamp",
                    "note": "assistant: ignore this element",
                }
            ]
        }
    }  # the compact stored form: defaults left out
    assert watch["patch"]["filter"]["watch"][0]["selector"] == "p.stamp"

    text = choice(out, "ignore_text")["rule"]
    assert text["type"] == "text" and text["pattern_kind"] == "regex"
    assert text["pattern"] == r"Updated\s+\d+\s+minutes\s+ago"  # numbers generalised
    to_here, from_here = (
        choice(out, "ignore_to_here")["rule"],
        choice(out, "ignore_from_here")["rule"],
    )
    assert to_here["end"] == "Updated 5 minutes ago" and to_here["start"] is None
    assert from_here["start"] == "Updated 5 minutes ago" and from_here["end"] is None
    assert to_here["inclusive"] and from_here["inclusive"]
    assert all(c["explanation"] for c in out["choices"])


async def test_ignore_this_element_hides_it_from_comparison(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await setup(client, engine, clock, site)
    out = await assist(client, bid, path=PATHS["promo"], text="Sponsored: buy now")
    picked = choice(out, "ignore_element")
    assert picked["rule"]["selector"] == "li.promo"
    assert "Sponsored: buy now" in await blocks_of(client, bid)
    r = await client.post(
        f"/bookmarks/{bid}/test-filter", json={"filter": picked["patch"]["filter"]}
    )
    assert "Sponsored: buy now" not in r.json()["latest"] and "Story two" in r.json()["latest"]
    await client.patch(f"/bookmarks/{bid}", json=picked["patch"])
    await engine.drain_background()
    site.set("/d", digest().replace("buy now", "act fast"))
    await advance(engine, clock, 130, step=10)
    assert not toasts.shown  # the sponsored line changed, and nobody is told


async def test_watch_only_this_element(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await setup(client, engine, clock, site)
    out = await assist(client, bid, path=PATHS["stamp"], text="")
    picked = choice(out, "watch_element")
    await client.patch(f"/bookmarks/{bid}", json=picked["patch"])
    await engine.drain_background()
    assert await blocks_of(client, bid) == ["Updated 5 minutes ago"]
    site.set("/d", digest(visitors="9,999"))  # outside the watched element
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown
    site.set("/d", digest(minutes=6))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1


async def test_ignore_text_like_this_makes_a_changing_timestamp_stop_alerting(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await setup(client, engine, clock, site)
    site.set("/d", digest(minutes=6))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # the false positive the user is looking at
    out = await assist(client, bid, path=PATHS["stamp"], text="Updated 6 minutes ago")
    picked = choice(out, "ignore_text")
    assert picked["rule"]["pattern"] == r"Updated\s+\d+\s+minutes\s+ago" and not picked["warnings"]
    assert "1 block now" in picked["explanation"]
    await client.patch(f"/bookmarks/{bid}", json=picked["patch"])
    await engine.drain_background()
    for minutes in (7, 42, 1000):
        site.set("/d", digest(minutes=minutes))
        await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # whatever the number, it no longer counts
    site.set("/d", digest(minutes=1000, extra="<p>Real news</p>"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 2


async def test_ignore_from_page_start_to_here_and_from_here_to_page_end(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    bid = await setup(client, engine, clock, site)
    out = await assist(client, bid, path=PATHS["welcome"], text="Welcome to the digest")
    to_here = choice(out, "ignore_to_here")
    assert to_here["rule"]["end"] == "Welcome to the digest" and not to_here["warnings"]
    r = await client.post(
        f"/bookmarks/{bid}/test-filter", json={"filter": to_here["patch"]["filter"]}
    )
    assert r.json()["latest"][0] == "Story one"  # header, nav, stamp and the selection are gone
    assert "Visitors today: 1,234" in r.json()["latest"]
    from_here = choice(out, "ignore_from_here")
    r = await client.post(f"/bookmarks/{bid}/test-filter",
                          json={"filter": from_here["patch"]["filter"]})  # fmt: skip
    assert r.json()["latest"] == ["Daily Digest", "Home | News", "Updated 5 minutes ago"]

    # applied: the footer counter below the cut no longer alerts
    await client.patch(f"/bookmarks/{bid}", json=from_here["patch"])
    await engine.drain_background()
    site.set("/d", digest(visitors="5,000"))
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown
    site.set("/d", digest(minutes=9))  # above the cut: still watched
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1


# -- markers --------------------------------------------------------------------------------------


async def test_a_marker_is_extended_to_the_whole_block_when_the_selection_is_not_unique(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    out = await assist(client, bid, path=PATHS["price1"], text="$5")  # "$5" is in two paragraphs
    from_here = choice(out, "ignore_from_here")
    assert from_here["rule"]["start"] == "Price: $5 today" and not from_here["warnings"]


async def test_a_marker_that_cannot_be_made_unique_says_so(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    # "Story one" is a list item and, again, the last paragraph. The last one cannot be told apart.
    out = await assist(client, bid, path=PATHS["last_story"], text="Story one")
    warning = choice(out, "ignore_from_here")["warnings"][0]
    assert "appears 2 times" in warning and "first occurrence" in warning
    first = await assist(client, bid, path="/html[1]/body[1]/div[1]/ul[1]/li[1]", text="Story one")
    assert "which is the one you selected" in choice(first, "ignore_from_here")["warnings"][0]
    # text that is not in the page at all
    gone = await assist(client, bid, path=PATHS["welcome"], text="Never on this page")
    for id_ in ("ignore_text", "ignore_to_here", "ignore_from_here"):
        assert "not in the filtered page" in choice(gone, id_)["warnings"][0]


async def test_multi_line_selections_use_the_first_and_last_lines_as_markers(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    out = await assist(
        client, bid, path=PATHS["welcome"], end_path=PATHS["story_two"],
        text="Welcome to the digest\nStory one\n  Sponsored: buy now\nStory two",
    )  # fmt: skip
    assert choice(out, "ignore_from_here")["rule"]["start"] == "Welcome to the digest"
    assert choice(out, "ignore_to_here")["rule"]["end"] == "Story two"
    assert "several lines" in " ".join(choice(out, "ignore_text")["warnings"])
    assert out["element"]["tag"] == "div" and out["element"]["selector"] == "#content"
    assert "spans several elements" in " ".join(choice(out, "ignore_element")["warnings"])


# -- widening, whole pages, the effective lists ------------------------------------------------


async def test_widening_to_the_parent_and_stopping_at_the_body(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set(
        "/d",
        '<html><body><div class="box"><p><span class="n">12</span> views</p></div></body></html>',
    )
    bid = await add(client, site)
    await settle(engine, clock)
    path = "/html[1]/body[1]/div[1]/p[1]/span[1]"
    steps = []
    for n in range(4):
        out = await assist(client, bid, path=path, text="12", levels_up=n)
        steps.append(
            (out["element"]["tag"], out["element"]["levels_up"], out["element"]["can_widen"])
        )
    assert steps == [("span", 0, True), ("p", 1, True), ("div", 2, True), ("body", 3, False)]
    out = await assist(client, bid, path=path, text="12", levels_up=10)
    assert out["element"]["tag"] == "body"  # never past the body
    warnings = " ".join(choice(out, "ignore_element")["warnings"])
    assert "whole page" in warnings and "nothing to compare" in warnings
    assert "changes nothing" in " ".join(choice(out, "watch_element")["warnings"])


async def test_rules_are_added_to_the_effective_lists_so_inherited_ones_survive(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/d", digest())
    folder = (
        await client.post(
            "/folders",
            json={"name": "F", "defaults": {"filter": {"ignore": [
                {"type": "text", "pattern": "inherited", "pattern_kind": "literal"}]}}},
        )
    ).json()["id"]  # fmt: skip
    bid = await add(client, site, folder_id=folder)
    await settle(engine, clock)
    out = await assist(client, bid, path=PATHS["promo"], text="")
    patch = choice(out, "ignore_element")["patch"]["filter"]["ignore"]
    assert [r["pattern"] if "pattern" in r else r["selector"] for r in patch] == [
        "inherited",
        "li.promo",
    ]
    await client.patch(f"/bookmarks/{bid}", json=choice(out, "ignore_element")["patch"])
    effective = (await client.get(f"/bookmarks/{bid}")).json()["filter"]["ignore"]
    assert len(effective) == 2  # the folder's rule was not replaced by the new one


async def test_an_element_without_a_stable_selector_gets_an_xpath(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/d", "<html><body><div><p>one</p><p>two</p><p>three</p></div></body></html>")
    bid = await add(client, site)
    await settle(engine, clock)
    out = await assist(client, bid, path="/html[1]/body[1]/div[1]/p[2]", text="two")
    el = out["element"]
    assert el["selector_kind"] == "xpath" and el["selector"] == "/html[1]/body[1]/div[1]/p[2]"
    picked = choice(out, "ignore_element")
    assert picked["rule"]["selector_kind"] == "xpath"
    r = await client.post(
        f"/bookmarks/{bid}/test-filter", json={"filter": picked["patch"]["filter"]}
    )
    assert r.json()["latest"] == ["one", "three"]


# -- errors ----------------------------------------------------------------------------------------


async def test_a_text_only_request_still_offers_the_text_choices(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    out = await assist(client, bid, path="/html[1]/body[1]/gone[7]", text="Welcome to the digest")
    assert out["element"] is None
    assert [c["id"] for c in out["choices"]] == [
        "ignore_text",
        "ignore_to_here",
        "ignore_from_here",
    ]


async def test_bad_requests(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    url = f"/bookmarks/{bid}/filter-assist"
    for body in (
        {"path": "//div"}, {"path": "/html[1]/body[1]/div[1]; drop"}, {"path": "html/body"},
        {"path": "/html[1]/body[1]/div[1]", "end_path": "x"}, {"path": ""},
        {"path": PATHS["stamp"], "levels_up": 99},
    ):  # fmt: skip
        assert (await client.post(url, json=body)).status_code == 422, body
    gone = await client.post(url, json={"path": "/html[1]/body[1]/nothing[3]"})
    assert gone.status_code == 422 and "could not be found" in gone.text  # no element, no text
    assert (await client.post("/bookmarks/999/filter-assist", json={"path": PATHS["stamp"]})
            ).status_code == 404  # fmt: skip
    fresh = await client.post(
        "/bookmarks",
        json={
            "url": "http://127.0.0.1:1/never",
            "schedule": {"mode": "manual", "interval_s": 3600},
        },
    )
    r = await client.post(
        f"/bookmarks/{fresh.json()['id']}/filter-assist", json={"path": PATHS["stamp"]}
    )
    assert r.status_code == 409 and "no stored page" in r.text


async def test_the_old_version_can_be_inspected_too(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    site.set("/d", digest(minutes=6))
    await advance(engine, clock, 70, step=10)  # latest says 6, the last-read baseline says 5
    new = await assist(client, bid, path=PATHS["stamp"], text="", version="new")
    old = await assist(client, bid, path=PATHS["stamp"], text="", version="old")
    assert (
        new["selection"] == "Updated 6 minutes ago" and old["selection"] == "Updated 5 minutes ago"
    )


async def test_a_specific_stored_version_can_be_inspected_but_only_the_bookmarks_own(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    bid = await setup(client, engine, clock, site)
    site.set("/d", digest(minutes=6))
    await advance(engine, clock, 70, step=10)
    site.set("/d", digest(minutes=7))
    await advance(engine, clock, 70, step=10)
    changes = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    older = min(changes, key=lambda c: c["id"])  # the change that moved the page to "6 minutes"
    out = await assist(
        client, bid, path=PATHS["stamp"], text="", version_id=older["new_version_id"]
    )
    assert out["selection"] == "Updated 6 minutes ago"  # not the latest ("7")
    other = await add(client, site, page="/e", name="other")
    site.set("/e", digest(minutes=99))
    other_version = (await client.get(f"/bookmarks/{other}")).json()["latest_version_id"]
    r = await client.post(
        f"/bookmarks/{bid}/filter-assist",
        json={"path": PATHS["stamp"], "version_id": other_version or 10_000},
    )
    assert r.status_code == 409  # no such version of this bookmark
