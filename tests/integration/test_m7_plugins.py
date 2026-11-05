"""M7: plugins through the engine: every hook, isolation, state, the catalogue."""

from __future__ import annotations

from textwrap import dedent
from typing import Any

import httpx
import pytest

from pagewatch.engine import plugin_runtime
from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


def plugin(data_dir: DataDir, name: str, body: str) -> None:
    (data_dir.plugins_dir / f"{name}.py").write_text(dedent(body), encoding="utf-8")


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out  # newest first


async def changes(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    return list(reversed(out))


# -- the catalogue ----------------------------------------------------------------------------


async def test_the_shipped_examples_can_be_listed_and_installed(
    client: httpx.AsyncClient, data_dir: DataDir
) -> None:
    listing = {p["name"]: p for p in (await client.get("/plugins")).json()}
    assert set(listing) == {"price_range", "stale_page", "single_new_item", "pdf_links"}
    assert all(p["example"] and not p["installed"] and p["description"] for p in listing.values())

    r = await client.post("/plugins/install", json={"name": "price_range"})
    assert (
        r.status_code == 201 and r.json()["installed"] and r.json()["hooks"] == ["check_keywords"]
    )
    assert (data_dir.plugins_dir / "price_range.py").is_file()
    assert (await client.post("/plugins/install", json={"name": "price_range"})).status_code == 409
    again = await client.post("/plugins/install", json={"name": "price_range", "overwrite": True})
    assert again.status_code == 201
    assert (await client.post("/plugins/install", json={"name": "nope"})).status_code == 404

    plugin(data_dir, "mine", '"""My own."""\ndef compare(ctx, old, new): return None\n')
    plugin(data_dir, "broken", "def oops(:\n")
    plugin(data_dir, "_private", "x = 1\n")
    listing = {p["name"]: p for p in (await client.get("/plugins")).json()}
    assert listing["mine"]["hooks"] == ["compare"] and not listing["mine"]["example"]
    assert listing["mine"]["description"] == "My own."
    assert listing["broken"]["error"] and listing["broken"]["hooks"] == []
    assert "_private" not in listing


async def test_only_installed_plugins_can_be_assigned(
    client: httpx.AsyncClient, data_dir: DataDir
) -> None:
    r = await client.post("/bookmarks", json={"url": "https://example.com/", "plugin": "ghost"})
    assert r.status_code == 422 and "not installed" in r.text
    r = await client.post("/bookmarks", json={"url": "https://example.com/", "plugin": "../evil"})
    assert r.status_code == 422
    plugin(data_dir, "ok", "def compare(ctx, old, new): return None\n")
    bid = await add(client, "https://example.com/", plugin="ok")
    assert (await client.get(f"/bookmarks/{bid}")).json()["plugin"] == "ok"
    assert (await client.patch(f"/bookmarks/{bid}", json={"plugin": "ghost"})).status_code == 422
    assert (await client.patch(f"/bookmarks/{bid}", json={"plugin": None})).json()["plugin"] is None


# -- before_check and merge_pages ------------------------------------------------------------


async def test_before_check_can_skip_and_can_rewrite_the_request(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir,
) -> None:  # fmt: skip
    site.set("/p", article("one"))
    plugin(
        data_dir, "gate",
        """
        def before_check(ctx):
            if ctx.bookmark.info1 == "skip":
                return False
            ctx.request.headers["X-Plugin"] = "yes"
            ctx.request.url += "?via=plugin"
            ctx.request.body = "a=1"
        """,
    )  # fmt: skip
    bid = await add(client, site.url("/p"), plugin="gate", info1="skip")
    await settle(engine, clock)
    assert site.hits == [] and (await runs(client, bid))[0]["outcome"] == "skipped"
    assert (await runs(client, bid))[0]["reason"] == "plugin_skip"
    assert (await client.get(f"/bookmarks/{bid}")).json()["consecutive_errors"] == 0

    await client.patch(f"/bookmarks/{bid}", json={"info1": "go"})
    await advance(engine, clock, 70, step=10)
    (hit,) = site.hits_for("/p")
    assert hit.method == "POST" and hit.query == "via=plugin"
    assert {k.lower(): v for k, v in hit.headers.items()}["x-plugin"] == "yes"
    assert (await runs(client, bid))[0]["outcome"] == "first"


async def test_merge_pages_appends_what_the_plugin_returns(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/p", article("one"))
    plugin(
        data_dir, "more",
        """
        def merge_pages(ctx, page):
            extra = ctx.http_get(ctx.bookmark.url.replace("/p", "/more"))
            return ["<p>" + extra.text + "</p>", b"<p>and bytes</p>", 5]
        """,
    )  # fmt: skip
    site.set("/more", "<p>from the second page</p>")
    from pagewatch import plugin_api

    plugin_api.HTTP_GAP_S = 0.0
    bid = await add(client, site.url("/p"), plugin="more")
    await settle(engine, clock)
    new = (
        await client.get(f"/bookmarks/{bid}/diff", params={"view": "new", "format": "json"})
    ).json()
    assert "from the second page" in new["html"] and "and bytes" in new["html"]
    site.set("/more", "<p>changed second page</p>")
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "changed second page" in toasts.shown[0].body
    assert (await runs(client, bid))[0]["reason"] == "plugin_error:merge_pages"  # the int `5`


# -- preprocess, check_keywords, compare -------------------------------------------------------


async def test_preprocess_rewrites_the_content_before_extraction(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/p", "<html><body><p>keep</p><p>noise 1</p></body></html>")
    plugin(data_dir, "strip", "def preprocess(ctx, html): return html.replace('noise', 'N')\n")
    bid = await add(client, site.url("/p"), plugin="strip")
    await settle(engine, clock)
    site.set("/p", "<html><body><p>keep</p><p>noise 2</p></body></html>")
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # "N 1" -> "N 2" is still a change ...
    plugin(data_dir, "strip", "def preprocess(ctx, html): return '<p>constant</p>'\n")
    await client.patch(f"/bookmarks/{bid}", json={"info1": "touch"})  # re-saves nothing relevant
    await client.patch(f"/bookmarks/{bid}", json={"plugin": None})
    await client.patch(f"/bookmarks/{bid}", json={"plugin": "strip"})  # re-assigned: blocks rebuilt
    await engine.drain_background()
    site.set("/p", "<html><body><p>anything</p></body></html>")
    await advance(engine, clock, 130, step=10)
    assert len(toasts.shown) == 1  # ... but now everything preprocesses to the same page
    new = (
        await client.get(f"/bookmarks/{bid}/diff", params={"view": "new", "format": "json"})
    ).json()
    assert "constant" in new["html"] and "anything" not in new["html"]  # the viewer agrees


async def test_check_keywords_is_or_ed_with_the_built_in_rules(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    plugin(data_dir, "kw", "def check_keywords(ctx, text):\n    return 'urgent' in text.lower()\n")
    site.set("/a", article("one"))
    site.set("/b", article("one"))
    a = await add(client, site.url("/a"), plugin="kw", gate={"keywords": "sale"})
    b = await add(client, site.url("/b"), plugin="kw")  # no built-in rules: the plugin decides
    await settle(engine, clock)
    for path, extra in (("/a", "boring news"), ("/b", "boring news")):
        site.set(path, article("one", extra))
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown  # neither the rule nor the plugin liked it
    assert {r["reason"] for bid in (a, b) for r in (await runs(client, bid))[:1]} == {
        "keyword_miss"
    }
    site.set("/a", article("one", "boring news", "URGENT: recall"))
    site.set("/b", article("one", "boring news", "sale today", "URGENT: recall"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 2
    (ch_a,) = await changes(client, a)
    assert ch_a["keyword_hits"] == ["plugin:kw"]
    site.set("/a", article("one", "boring news", "URGENT: recall", "big sale"))
    await advance(engine, clock, 70, step=10)  # the built-in rule alone is enough, as before
    assert len(await changes(client, a)) == 2


async def test_compare_has_the_last_word(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    plugin(
        data_dir, "judge",
        """
        from pagewatch.plugin_api import Verdict
        def compare(ctx, old, new):
            text = " ".join(b for b in new if b not in old).lower()  # only what is new
            if "force" in text:
                return Verdict.change()
            if "mute" in text:
                return Verdict.no_change("muted by plugin")
        """,
    )  # fmt: skip
    site.set("/p", article("one"))
    bid = await add(client, site.url("/p"), plugin="judge", gate={"keywords": "never-matches"})
    await settle(engine, clock)
    site.set("/p", article("one", "plain change"))
    await advance(engine, clock, 70, step=10)
    assert (await runs(client, bid))[0]["reason"] == "keyword_miss" and not toasts.shown  # deferred
    site.set("/p", article("one", "plain change", "FORCE it"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and (await runs(client, bid))[0]["outcome"] == "changed"
    site.set("/p", article("one", "plain change", "FORCE it", "mute this", "never-matches"))
    await advance(engine, clock, 70, step=10)
    last = (await runs(client, bid))[0]
    assert (last["outcome"], last["reason"]) == ("suppressed", "muted by plugin")
    assert len(toasts.shown) == 1  # the keyword would have alerted; the plugin said no
    # the version was stored all the same: nothing is re-detected
    await advance(engine, clock, 200, step=20)
    assert len(toasts.shown) == 1


# -- on_change, after_check, state, properties, notifications ---------------------------------


async def test_on_change_after_check_state_properties_and_notifications(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    plugin(
        data_dir, "full",
        """
        def after_check(ctx, run):
            n = ctx.state.get("checks", 0) + 1
            ctx.state["checks"] = n
            ctx.set_property("info2", f"{n} checks, last {run.outcome}")
            if run.outcome == "changed":
                ctx.notify("Heads up", "from after_check")

        def on_change(ctx, change):
            ctx.state["last_change_id"] = change.id
            ctx.state["summary"] = change.summary
        """,
    )  # fmt: skip
    site.set("/p", article("one"))
    bid = await add(client, site.url("/p"), plugin="full")
    await settle(engine, clock)
    assert (await client.get(f"/bookmarks/{bid}")).json()["info2"] == "1 checks, last first"
    await advance(engine, clock, 70, step=10)
    assert (await client.get(f"/bookmarks/{bid}")).json()["info2"] == "2 checks, last unchanged"
    site.set("/p", article("one", "two"))
    await advance(engine, clock, 70, step=10)
    assert (await client.get(f"/bookmarks/{bid}")).json()["info2"] == "3 checks, last changed"
    assert {t.title for t in toasts.shown} == {"127.0.0.1", "Heads up"}  # the alert + the plugin's
    state = await engine.db.read(
        lambda c: c.execute("SELECT value_json FROM setting WHERE key=?",
                            (f"kv.plugin_state.{bid}",)).fetchone()[0]
    )  # fmt: skip
    assert '"checks":3' in state and '"last_change_id":1' in state and "two" in state
    await client.delete(f"/bookmarks/{bid}")
    gone = await engine.db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM setting WHERE key LIKE 'kv.plugin_state.%'")
        .fetchone()[0]
    )  # fmt: skip
    assert gone == 0  # the state went with the bookmark


async def test_private_bookmarks_get_no_plugin_notification_content(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    plugin(data_dir, "n", "def after_check(ctx, run): ctx.notify('T', 'secret page text')\n")
    site.set("/p", article("one"))
    await add(
        client, site.url("/p"), plugin="n", actions={"actions": [], "alert_privacy": "private"}
    )
    await settle(engine, clock)
    (toast,) = toasts.shown
    assert "secret" not in toast.body and "Open PageWatch" in toast.body


# -- isolation ---------------------------------------------------------------------------------


async def test_a_failing_plugin_never_fails_or_blocks_a_check(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, toasts: LogToastBackend,
) -> None:  # fmt: skip
    plugin(
        data_dir, "bad",
        """
        def before_check(ctx): raise RuntimeError("before")
        def preprocess(ctx, html): raise RuntimeError("pre")
        def check_keywords(ctx, text): raise RuntimeError("kw")
        def compare(ctx, old, new): raise RuntimeError("cmp")
        def on_change(ctx, change): raise RuntimeError("on")
        def after_check(ctx, run): raise RuntimeError("after")
        """,
    )  # fmt: skip
    site.set("/p", article("one"))
    bid = await add(client, site.url("/p"), plugin="bad")
    await settle(engine, clock)
    site.set("/p", article("one", "two"))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1  # the change was detected and alerted despite six raising hooks
    latest = (await runs(client, bid))[0]
    assert latest["outcome"] == "changed" and latest["reason"].startswith("plugin_error:")
    assert (await client.get(f"/bookmarks/{bid}")).json()["status"] == "changed"


async def test_a_slow_plugin_hook_is_flagged_in_the_check_log(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(plugin_runtime, "SLOW_HOOK_S", -1.0)
    plugin(data_dir, "slowpoke", "def after_check(ctx, run): pass\ndef before_check(ctx): pass\n")
    site.set("/p", article("one"))
    bid = await add(client, site.url("/p"), plugin="slowpoke")
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["reason"] == "plugin_slow:before_check"
    assert (await runs(client, bid))[0]["outcome"] == "first"


async def test_a_plugin_removed_after_assignment_does_not_stop_the_bookmark(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir,
) -> None:  # fmt: skip
    plugin(data_dir, "gone", "def compare(ctx, old, new): return None\n")
    site.set("/p", article("one"))
    bid = await add(client, site.url("/p"), plugin="gone")
    await settle(engine, clock)
    (data_dir.plugins_dir / "gone.py").unlink()
    await advance(engine, clock, 70, step=10)
    latest = (await runs(client, bid))[0]
    assert latest["outcome"] == "unchanged" and latest["reason"] == "plugin_missing:gone"


async def test_a_plugin_that_fails_to_import_is_noted_on_every_check(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir,
) -> None:  # fmt: skip
    plugin(data_dir, "oops", "def compare(ctx, old, new): return None\n")
    site.set("/p", article("one"))
    bid = await add(client, site.url("/p"), plugin="oops")
    plugin(data_dir, "oops", "import definitely_not_installed_anywhere\n")
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["reason"] == "plugin_error:load"
    assert (await runs(client, bid))[0]["outcome"] == "first"
