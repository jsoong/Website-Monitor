"""M7 acceptance: Follow-Links on the fixture site creates child bookmarks within its depth and page
limits, and the rules around it (robots.txt, patterns, de-duplication, inheritance)."""

from __future__ import annotations

from typing import Any

import httpx

from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite
from tests.support.sim import advance, settle

SCHED = {"interval_s": 3600, "jitter_pct": 0}


def page(title: str, *links: str, extra: str = "", head: str = "") -> str:
    anchors = "".join(f'<li><a href="{h}">{t}</a></li>' for t, h in (ln.split("|") for ln in links))
    return (
        f"<html><head><title>{title}</title>{head}</head><body><h1>{title}</h1>"
        f"<ul>{anchors}</ul>{extra}</body></html>"
    )


async def crawl(engine: Engine, clock: FakeClock) -> None:
    """Let checks run, discovery finish, and the children's first checks run, until quiet."""
    for _ in range(4):
        await settle(engine, clock)
        await engine.drain_background()
    await settle(engine, clock)


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def everything(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get("/bookmarks", params={"limit": 500})).json()[
        "items"
    ]
    return out


async def children(client: httpx.AsyncClient, parent: int) -> list[dict[str, Any]]:
    """The bookmarks in the folder generated for ``parent``."""
    state = await client.get(f"/bookmarks/{parent}")
    folders = {f["id"]: f for f in (await client.get("/folders")).json()}
    name = f"{state.json()['name']} (followed)"
    mine = [f for f in folders.values() if f["name"] == name]
    if not mine:
        return []
    out: list[dict[str, Any]] = (
        await client.get("/bookmarks", params={"folder": mine[0]["id"], "limit": 500})
    ).json()["items"]
    return out


def urls(items: list[dict[str, Any]]) -> list[str]:
    return sorted(i["url"].split("127.0.0.1:")[1].split("/", 1)[1] for i in items)


def hub(site: FixtureSite, *, extra_links: tuple[str, ...] = ()) -> None:
    site.set(
        "/hub",
        page(
            "Hub",
            "First story|/news/1", "Second story|/news/2", "Third story|/news/3",
            "Second again|/news/2#comments", "About us|/about", f"Elsewhere|http://localhost:{site.port}/news/9",
            "Mail|mailto:a@b.c", "Script|javascript:void(0)", "Self|/hub", "Same|/hub#top",
            *extra_links,
        ),
    )  # fmt: skip
    for n in (1, 2, 3, 4):
        site.set(f"/news/{n}", page(f"Story {n}"))
    site.set("/about", page("About"))


# -- the basics -------------------------------------------------------------------------------


async def test_children_are_created_from_matching_links_in_a_generated_folder(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    parent = await add(
        client, site.url("/hub"), name="Hub",
        fetch={"follow": {"include": ["/news/*"]}},
    )  # fmt: skip
    await crawl(engine, clock)
    kids = await children(client, parent)
    assert urls(kids) == ["news/1", "news/2", "news/3"]  # the pattern, no fragment twin, no self
    assert [k["name"] for k in sorted(kids, key=lambda k: k["url"])] == [
        "First story", "Second story", "Third story",
    ]  # fmt: skip
    folder = next(f for f in (await client.get("/folders")).json() if f["name"] == "Hub (followed)")
    assert folder["parent_id"] is None and not folder["is_virtual"]
    # each child was checked once, as a followed page, and stores a baseline
    for kid in kids:
        runs = (await client.get(f"/bookmarks/{kid['id']}/runs")).json()["items"]
        assert [(r["trigger"], r["outcome"]) for r in runs] == [("follow", "first")]
    assert len(await everything(client)) == 4  # the parent and three children, nothing else


async def test_children_inherit_the_parents_settings_and_follow_its_edits(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    outer = (await client.post("/folders", json={"name": "Shops"})).json()["id"]
    parent = await add(
        client, site.url("/hub"), name="Hub", folder_id=outer,
        schedule={"interval_s": 7200, "jitter_pct": 0}, gate={"keywords": "price", "min_chars": 5},
        filter={"ignore": [{"type": "text", "pattern": "ads", "pattern_kind": "literal"}]},
        fetch={"follow": {"include": ["/news/1", "/news/2"]}, "timeout_s": 12},
        actions={"actions": [{"type": "toast"}], "alert_privacy": "private"},
        highlight_mode="exact", priority=1, info1="parent only",
    )  # fmt: skip
    await crawl(engine, clock)
    kids = await children(client, parent)
    assert len(kids) == 2
    folder = next(f for f in (await client.get("/folders")).json() if f["name"] == "Hub (followed)")
    assert folder["parent_id"] == outer  # next to the parent
    kid = (await client.get(f"/bookmarks/{kids[0]['id']}")).json()
    assert kid["schedule"]["interval_s"] == 7200 and kid["gate"]["keywords"] == "price"
    assert kid["gate"]["min_chars"] == 5 and kid["fetch"]["timeout_s"] == 12
    assert kid["filter"]["ignore"][0]["pattern"] == "ads"
    assert kid["actions"]["alert_privacy"] == "private"
    assert kid["highlight_mode"] == "exact" and kid["priority"] == 1
    assert kid["fetch"]["follow"] is None and kid["fetch"]["merge"] is None  # no crawling onward
    assert kid["info1"] is None and kid["overrides"]["gate"] == {}  # inherited, not copied

    # edit the parent: its children follow (they inherit through the generated folder)
    r = await client.patch(f"/bookmarks/{parent}", json={"gate": {"keywords": "sale"}})
    assert r.status_code == 200
    await engine.drain_background()
    kid = (await client.get(f"/bookmarks/{kids[0]['id']}")).json()
    assert kid["gate"]["keywords"] == "sale"


async def test_the_page_limit_and_a_later_new_link(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    parent = await add(
        client, site.url("/hub"), name="Hub",
        fetch={"follow": {"include": ["/news/*"], "max_pages": 2}},
    )  # fmt: skip
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/1", "news/2"]  # document order, capped
    # the page gains a link, but the budget is spent: nothing more is ever created
    hub(site, extra_links=("Fourth|/news/4",))
    await advance(engine, clock, 3600, step=1800)
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/1", "news/2"]
    # raising the limit lets the stored page be looked at again, without waiting for a change
    await client.patch(f"/bookmarks/{parent}", json={"fetch": {"follow": {"max_pages": 4}}})
    await engine.drain_background()
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/1", "news/2", "news/3", "news/4"]


async def test_patterns_same_host_and_exclusions(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    site.set("/news/9", page("Elsewhere"))  # served by this server, but under another host name
    wild = await add(
        client, site.url("/hub"), name="Wild",
        fetch={"follow": {"exclude": ["/news/2", "*about*"], "folder_name": "Wild kids"}},
    )  # fmt: skip
    regex = await add(
        client, site.url("/hub") + "?r=1", name="Regex",
        fetch={"follow": {"pattern_kind": "regex", "include": [r"/news/[13]$"]}},
    )  # fmt: skip
    away = await add(
        client, site.url("/hub") + "?a=1", name="Away",
        fetch={"follow": {"same_host": False, "include": ["localhost"]}},
    )  # fmt: skip
    await crawl(engine, clock)
    folders = {f["name"]: f["id"] for f in (await client.get("/folders")).json()}
    wild_kids = (await client.get("/bookmarks", params={"folder": folders["Wild kids"]})).json()
    assert urls(wild_kids["items"]) == ["news/1", "news/3"]  # /news/2 and /about excluded
    assert urls(await children(client, regex)) == ["news/1", "news/3"]
    (far,) = await children(client, away)
    assert far["url"] == f"http://localhost:{site.port}/news/9"  # another host: the owner asked
    assert wild and regex


async def test_nothing_is_created_when_follow_is_off_or_the_page_says_nofollow(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    site.set("/polite", page("Polite", "A|/news/1", head='<meta name="robots" content="NOFOLLOW">'))
    off = await add(client, site.url("/hub"), name="Off", fetch={"follow": {"enabled": False}})
    polite = await add(client, site.url("/polite"), name="Polite", fetch={"follow": {}})
    plain = await add(client, site.url("/about"), name="Plain")
    await crawl(engine, clock)
    assert [await children(client, b) for b in (off, polite, plain)] == [[], [], []]
    assert len(await everything(client)) == 3


# -- de-duplication and memory --------------------------------------------------------------


async def test_existing_bookmarks_are_not_duplicated_and_deleted_children_stay_deleted(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    mine = await add(client, site.url("/news/2/"), name="Already mine")  # trailing slash: same page
    parent = await add(client, site.url("/hub"), name="Hub",
                       fetch={"follow": {"include": ["/news/*"]}})  # fmt: skip
    await crawl(engine, clock)
    kids = await children(client, parent)
    assert urls(kids) == ["news/1", "news/3"] and mine not in {k["id"] for k in kids}

    victim = next(k for k in kids if k["url"].endswith("/news/1"))
    await client.delete(f"/bookmarks/{victim['id']}")
    hub(site, extra_links=("Fourth|/news/4",))  # the page changes, so discovery runs again
    await advance(engine, clock, 3600, step=1800)
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/3", "news/4"]  # 1 stays deleted


# -- robots.txt -------------------------------------------------------------------------------


async def test_robots_txt_is_obeyed_for_discovered_links_only(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/robots.txt", "User-agent: *\nDisallow: /private/\n\nUser-agent: PageWatch\n"
                            "Disallow: /news/2\n", content_type="text/plain")  # fmt: skip
    hub(site)
    site.set("/private/doc", page("Private"))
    site.set("/hub", page("Hub", "One|/news/1", "Two|/news/2", "Secret|/private/doc"))
    parent = await add(client, site.url("/hub"), name="Hub", fetch={"follow": {}})
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/1", "private/doc"]
    # PageWatch's own group wins over "*": /news/2 is out, /private/ is allowed for it
    assert len(site.hits_for("/robots.txt")) == 1  # fetched once and cached


async def test_a_page_the_owner_chose_is_not_checked_against_robots_txt(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/robots.txt", "User-agent: *\nDisallow: /\n", content_type="text/plain")
    site.set("/mine", page("Mine"))
    bid = await add(client, site.url("/mine"), name="Mine")
    await crawl(engine, clock)
    runs = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    assert runs[0]["outcome"] == "first" and site.hits_for("/robots.txt") == []


async def test_a_broken_robots_txt_server_blocks_discovery_until_it_recovers(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)
    site.set("/robots.txt", "boom", status=503, content_type="text/plain")
    parent = await add(client, site.url("/hub"), name="Hub",
                       fetch={"follow": {"include": ["/news/*"]}})  # fmt: skip
    await crawl(engine, clock)
    assert await children(client, parent) == []  # RFC 9309: unreachable means disallowed
    site.set("/robots.txt", "User-agent: *\nDisallow:\n", content_type="text/plain")
    await client.patch(f"/bookmarks/{parent}", json={"fetch": {"follow": {"max_pages": 50}}})
    await crawl(engine, clock)
    assert await children(client, parent) == []  # the failure is remembered for a while ...
    clock.advance(16 * 60)  # ... 15 minutes
    await client.patch(f"/bookmarks/{parent}", json={"fetch": {"follow": {"max_pages": 51}}})
    await engine.drain_background()
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/1", "news/2", "news/3"]


async def test_no_robots_txt_means_everything_is_allowed(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    hub(site)  # the fixture site answers 404 for /robots.txt
    parent = await add(client, site.url("/hub"), name="Hub",
                       fetch={"follow": {"include": ["/news/*"], "max_pages": 1}})  # fmt: skip
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["news/1"]


# -- the acceptance scenario ----------------------------------------------------------------


async def test_follow_links_stays_within_its_depth_and_page_limits(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    """A three-level site (hub -> sections -> articles -> comments); the crawl is asked for depth
    2 and at most 5 pages."""
    site.set("/hub", page("Hub", *(f"Section {s}|/s{s}" for s in (1, 2, 3))))
    for s in (1, 2, 3):
        site.set(
            f"/s{s}", page(f"Section {s}", *(f"Article {s}.{a}|/s{s}/a{a}" for a in (1, 2, 3)))
        )
        for a in (1, 2, 3):
            site.set(f"/s{s}/a{a}", page(f"Article {s}.{a}", f"Comment|/s{s}/a{a}/comments"))
            site.set(f"/s{s}/a{a}/comments", page("Comments"))
    root = await add(
        client, site.url("/hub"), name="Hub",
        fetch={"follow": {"include": ["/s*"], "depth": 2, "max_pages": 5}},
    )  # fmt: skip
    await crawl(engine, clock)

    created = {b["url"].split("/", 3)[3] for b in await everything(client)} - {"hub"}
    assert len(created) == 5  # the page limit holds over the whole crawl
    assert created >= {"s1", "s2", "s3"}  # depth 1: the sections come first
    deeper = created - {"s1", "s2", "s3"}
    assert len(deeper) == 2 and all(u.count("/") == 1 for u in deeper)  # depth 2: articles only
    assert not any(u.endswith("comments") for u in created)  # depth 3 is never reached

    # more crawling later never exceeds the budget either
    await advance(engine, clock, 3 * 3600, step=3600)
    await crawl(engine, clock)
    assert len(await everything(client)) == 1 + 5
    assert (await client.get(f"/bookmarks/{root}")).status_code == 200


async def test_depth_one_is_the_default_and_the_first_level_only(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/hub", page("Hub", "Section|/s1"))
    site.set("/s1", page("Section 1", "Article|/s1/a1"))
    site.set("/s1/a1", page("Article"))
    parent = await add(client, site.url("/hub"), name="Hub", fetch={"follow": {}})
    await crawl(engine, clock)
    assert urls(await children(client, parent)) == ["s1"]
    assert len(await everything(client)) == 2  # not the article
