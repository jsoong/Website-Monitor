"""M2 acceptance criteria, end to end through the engine under a fake clock."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    r = await client.post("/bookmarks", json={"url": url, "schedule": SCHED, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def changes(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    return list(reversed(out))


# -- false positive -> automatic filter -------------------------------------------------


async def test_false_positive_flag_produces_a_filter_that_stops_the_alert_on_the_next_check(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set_dynamic(
        "/news",
        lambda n: (
            "<html><body><h1>Daily News</h1>"
            f"<p>Updated {4 + n} minutes ago</p><p>Council approves budget</p></body></html>"
        ),
    )
    bid = await add(client, site.url("/news"), name="News")
    await settle(engine, clock)
    await advance(engine, clock, 70, step=10)  # the ticking timestamp is detected as a change
    assert len(toasts.shown) == 1
    (change,) = await changes(client, bid)

    r = await client.post(f"/changes/{change['id']}/false-positive")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["resolves_all"] is True and out["remaining_changed_blocks"] == 0
    top = out["proposals"][0]
    assert top["kind"] == "volatile_pattern" and top["pattern_name"] == "relative_time"
    assert top["verified"] is True and top["rule"]["type"] == "text"
    assert "minutes ago" in top["example_new"] or "minutes" in top["example_new"]
    assert (await changes(client, bid))[0]["feedback"] == "false_positive"

    # the user confirms: apply the ready-made patch
    r = await client.patch(f"/bookmarks/{bid}", json=out["patch"])
    assert r.status_code == 200, r.text
    await engine.drain_background()  # versions re-normalised with the new filter
    toasts.shown.clear()
    await advance(engine, clock, 600, step=20)  # ten more checks, the timestamp ticks every time
    assert not toasts.shown
    runs = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    assert (
        sum(1 for x in runs if x["outcome"] == "changed") == 1
    )  # only the original false positive
    assert (await client.get(f"/bookmarks/{bid}")).json()["status"] in ("ok", "changed")

    # a *real* change still gets through
    site.set("/news", "<html><body><h1>Daily News</h1><p>Mayor resigns</p></body></html>")
    await advance(engine, clock, 90, step=10)
    assert len(toasts.shown) == 1 and "Mayor resigns" in toasts.shown[0].body


# -- keyword rules end to end -----------------------------------------------------------

CARD = (
    "<html><body><div class='card'><h2>NVIDIA RTX 4090 Founders Edition</h2>"
    "<p class='price'>{price}</p><p>{stock}</p></div></body></html>"
)
RULE = 'page("RTX 4090") + num(\\$([\\d,.]+)) < 1200'


async def test_price_drop_on_the_product_card_fires_page_plus_num_rule(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/card", CARD.format(price="$1,299.00", stock="In stock"))
    bid = await add(client, site.url("/card"), name="GPU", gate={"keywords": RULE})
    await settle(engine, clock)

    site.set("/card", CARD.format(price="$1,249.00", stock="In stock"))  # cheaper, not enough
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown
    runs = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    assert runs[0]["outcome"] == "suppressed" and runs[0]["reason"] == "keyword_miss"

    site.set("/card", CARD.format(price="$1,099.00", stock="In stock"))  # below the target
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "$1,099.00" in toasts.shown[0].body
    (change,) = await changes(client, bid)
    assert change["keyword_hits"] == [RULE]

    site.set("/card", CARD.format(price="$1,099.00", stock="Only 2 left"))  # not a price change
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # the rule needs a changed price below the target


async def test_in_stock_returning_after_out_of_stock_alerts_with_no_special_option(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/w", article("In stock", title="Widget"))
    bid = await add(client, site.url("/w"), name="Widget", gate={"keywords": '"in stock"'})
    await settle(engine, clock)
    site.set("/w", article("Out of stock", title="Widget"))
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown  # a real change, but not one the keyword asked for
    site.set("/w", article("In stock", title="Widget"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # flipping back fires
    (change,) = await changes(client, bid)
    assert change["keyword_hits"] == ['"in stock"']


# -- cumulative threshold: hourly vs after a sleep --------------------------------------

BASE = ["<h1>Council notes</h1>", "<p>Meeting minutes</p>"]
ADDS = ["alpha", "beta", "gamma", "delta"]


def notes(k: int) -> str:
    return (
        "<html><body>" + "".join(BASE) + "".join(f"<p>{w}</p>" for w in ADDS[:k]) + "</body></html>"
    )


async def test_four_small_additions_fire_one_cumulative_alert_checked_hourly(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/n", notes(0))
    bid = await add(
        client, site.url("/n"), name="Council",
        schedule={"interval_s": 3600, "jitter_pct": 0},
        gate={"min_changed_words": 4, "threshold_mode": "cumulative"},
    )  # fmt: skip
    await settle(engine, clock)
    for k in range(1, 4):
        site.set("/n", notes(k))
        await advance(engine, clock, 3600 + 10, step=600)
        assert not toasts.shown, f"alerted too early after {k} additions"
    site.set("/n", notes(4))
    await advance(engine, clock, 3600 + 10, step=600)
    assert len(toasts.shown) == 1
    (change,) = await changes(client, bid)
    assert change["checks_accumulated"] == 4 and change["added_words"] == 4
    assert toasts.shown[0].body.startswith("(over 4 checks)")
    assert "alpha" in toasts.shown[0].body and "delta" in toasts.shown[0].body
    # and nothing further while the page stays as it is
    await advance(engine, clock, 3 * 3600, step=1800)
    assert len(toasts.shown) == 1


async def test_four_small_additions_fire_one_alert_when_checked_once_after_a_sleep(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/n", notes(0))
    bid = await add(
        client, site.url("/n"), name="Council",
        schedule={"interval_s": 3600, "jitter_pct": 0},
        gate={"min_changed_words": 4, "threshold_mode": "cumulative"},
    )  # fmt: skip
    await settle(engine, clock)
    hits_before = len(site.hits)
    for k in range(1, 5):  # the page grows while the laptop sleeps: nobody checks
        site.set("/n", notes(k))
    clock.advance(5 * 3600)  # wake up, hours overdue
    await settle(engine, clock)
    assert len(site.hits) == hits_before + 1  # one check, not one per missed interval
    assert len(toasts.shown) == 1
    (change,) = await changes(client, bid)
    assert change["checks_accumulated"] == 1 and change["added_words"] == 4
    assert not toasts.shown[0].body.startswith("(over")  # one check: no label


async def test_per_check_mode_misses_what_cumulative_catches(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/n", notes(0))
    await add(
        client, site.url("/n"), schedule={"interval_s": 3600, "jitter_pct": 0},
        gate={"min_changed_words": 4, "threshold_mode": "per_check"},
    )  # fmt: skip
    await settle(engine, clock)
    for k in range(1, 5):
        site.set("/n", notes(k))
        await advance(engine, clock, 3600 + 10, step=600)
    assert not toasts.shown  # each check changed one word: never reaches 4 in a single check


# -- mark read resets the anchor --------------------------------------------------------


async def test_mark_read_moves_the_anchor_so_thresholds_restart(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/n", notes(0))
    bid = await add(
        client, site.url("/n"), gate={"min_changed_words": 2, "threshold_mode": "cumulative"}
    )
    await settle(engine, clock)
    site.set("/n", notes(1))
    await advance(engine, clock, 70, step=10)  # +1 word: below 2
    await client.post(f"/bookmarks/{bid}/read")  # the user looks at the page: anchor = latest
    site.set("/n", notes(2))
    await advance(engine, clock, 70, step=10)  # +1 more word since the read: still below 2
    assert not toasts.shown
    site.set("/n", notes(3))
    await advance(engine, clock, 70, step=10)  # now 2 words since the read
    assert len(toasts.shown) == 1


# -- test filter ------------------------------------------------------------------------


def counts(engine: Engine) -> tuple[int, int, int, int]:
    def read(c: sqlite3.Connection) -> tuple[int, int, int, int]:
        n = lambda t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: E731
        return n("version"), n("change"), n("check_run"), n("action_job")

    return engine.db.read_sync(read)


async def test_test_filter_previews_a_candidate_config_and_persists_nothing(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    pages = iter(
        [
            "<html><body><h1>Shop</h1><p>Updated 5 minutes ago</p><p>Tea $4</p></body></html>",
            "<html><body><h1>Shop</h1><p>Updated 9 minutes ago</p><p>Tea $3</p></body></html>",
        ]
    )
    site.set_dynamic("/s", lambda n: next(pages))
    bid = await add(client, site.url("/s"), gate={"keywords": "$3"})  # the new price
    await settle(engine, clock)
    # nothing to compare yet: baseline == latest
    r = await client.post(f"/bookmarks/{bid}/test-filter", json={})
    assert r.status_code == 200 and r.json()["identical"] is True

    await client.post(f"/bookmarks/{bid}/check")  # force the second version
    await settle(engine, clock)
    stored = counts(engine)
    overrides_before = (await client.get(f"/bookmarks/{bid}")).json()["overrides"]

    current = (await client.post(f"/bookmarks/{bid}/test-filter", json={})).json()
    assert current["identical"] is False and current["alert"] is True
    assert any("Updated" in line for line in current["marks"]) and any(
        "Tea" in x for x in current["marks"]
    )

    candidate = {
        "filter": {
            "ignore": [{"type": "text", "pattern": "Updated * ago", "pattern_kind": "wildcard"}]
        }
    }
    out = (await client.post(f"/bookmarks/{bid}/test-filter", json=candidate)).json()
    assert not any("Updated" in b for b in out["baseline"] + out["latest"])  # filtered away
    assert [m for m in out["marks"] if m.startswith("~")] == ["~ Tea $[-4-]{+3+}"]
    assert out["alert"] is True and out["keyword_hits"] == ["$3"]

    # a candidate that makes the change disappear entirely
    quiet = {"filter": {"ignore": [{"type": "selector", "selector": "p"}]}}
    out = (await client.post(f"/bookmarks/{bid}/test-filter", json=quiet)).json()
    assert out["identical"] is False or out["alert"] is False
    assert out["reason"] in ("unchanged", "keyword_miss", "reorder_only")

    # the gate is part of the candidate too
    strict = {"gate": {"keywords": "nonexistent"}}
    out = (await client.post(f"/bookmarks/{bid}/test-filter", json=strict)).json()
    assert out["alert"] is False and out["reason"] == "keyword_miss"

    assert counts(engine) == stored  # nothing was written
    assert (await client.get(f"/bookmarks/{bid}")).json()["overrides"] == overrides_before


async def test_test_filter_errors(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    assert (await client.post("/bookmarks/99/test-filter", json={})).status_code == 404
    site.set("/x", article("a"))
    bid = await add(client, site.url("/x"), schedule={"mode": "manual"})
    await settle(engine, clock)
    bad = await client.post(
        f"/bookmarks/{bid}/test-filter",
        json={"filter": {"ignore": [{"type": "selector", "selector": "p["}]}},
    )
    assert bad.status_code == 422 and "selector" in bad.text
    bad = await client.post(f"/bookmarks/{bid}/test-filter", json={"gate": {"keywords": "regex("}})
    assert bad.status_code == 422
    fresh = await add(client, "http://127.0.0.1:9/never", schedule={"mode": "manual"})
    await settle(engine, clock)
    assert (await client.post(f"/bookmarks/{fresh}/test-filter", json={})).status_code == 409
    assert (await client.post("/changes/999/false-positive")).status_code == 404
