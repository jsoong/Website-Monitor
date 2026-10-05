"""M4 acceptance, the browser paths, end to end through the engine with scripted fetchers
standing in for the browser (the real Chromium is exercised in tests/browser)."""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any

import httpx
from PIL import Image

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.models import FetchErrorKind
from tests.support.docs import make_pdf, page_png, png_bytes
from tests.support.fakes import FakeLauncher, ScriptedFetcher, error, ok
from tests.support.fixture_site import FixtureSite
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}
SHELL = (
    '<html><head><title>Lotteries</title></head><body><div id="root"></div>'
    '<script src="/app.js"></script></body></html>'
)


def rendered(*lines: str) -> str:
    items = "".join(f"<p>{line}</p>" for line in lines)
    return f'<html><body><div id="root"><h1>Latest lotteries</h1>{items}</div></body></html>'


R1 = rendered("Sunset Terrace is open for applications until the end of November.",
              "Harbor View opens for applications next spring.")  # fmt: skip


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out


# -- method auto-detection --------------------------------------------------------------


async def test_the_js_only_fixture_is_detected_and_switched_to_the_browser_automatically(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/app", SHELL)
    state = {"html": R1}
    browser = ScriptedFetcher(lambda req, n: ok(req, state["html"]))
    engine.fetchers.replace("browser", browser)  # type: ignore[arg-type]
    bid = await add(client, site.url("/app"), name="Lotteries")
    assert (await client.get(f"/bookmarks/{bid}")).json()["check_method"] == "auto"
    await settle(engine, clock)

    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["check_method"] == "browser"  # persisted
    first = (await runs(client, bid))[0]
    assert first["outcome"] == "first" and first["method"] == "browser"
    assert first["reason"] == "auto_browser:app_shell"  # and the log says why
    assert browser.calls == 1 and len(site.hits_for("/app")) == 1
    assert engine.scheduler._entries[bid].browser is True  # next time it takes a browser slot
    new = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    assert "Latest lotteries" in new and "app.js" not in new  # the rendered page, not the shell

    # later checks go straight to the browser; the static fetcher is not used again
    await advance(engine, clock, 130, step=10)
    assert browser.calls >= 3 and len(site.hits_for("/app")) == 1
    assert not toasts.shown
    state["html"] = rendered("Sunset Terrace is open for applications until the end of November.",
                             "Harbor View opens for applications next spring.",
                             "Riverside Commons lottery opens Monday.")  # fmt: skip
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Riverside Commons" in toasts.shown[0].body


async def test_when_no_browser_works_the_first_check_fails_loudly_and_retries(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/app", SHELL)
    up = {"on": False}

    def respond(req: Any, n: int) -> Any:
        if not up["on"]:
            return error(
                req, FetchErrorKind.BROWSER, "no browser could be started: no edge; no chromium"
            )
        return ok(req, R1)

    engine.fetchers.replace("browser", ScriptedFetcher(respond))  # type: ignore[arg-type]
    bid = await add(client, site.url("/app"), gate={"error_threshold": 1})
    await settle(engine, clock)
    last = (await runs(client, bid))[0]
    assert (
        last["outcome"] == "error" and last["reason"] == "browser" and last["method"] == "browser"
    )
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["status"] == "error" and b["check_method"] == "auto" and b["latest_version_id"] is None
    assert len(toasts.shown) == 1 and "no browser could be started" in toasts.shown[0].body
    up["on"] = True  # Edge is repaired / Chromium downloaded
    await advance(engine, clock, 70, step=10)
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["check_method"] == "browser" and b["status"] == "ok" and b["latest_version_id"]


async def test_pages_that_do_not_need_a_browser_never_touch_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    browser = ScriptedFetcher(lambda req, n: ok(req, R1))
    engine.fetchers.replace("browser", browser)  # type: ignore[arg-type]
    words = " ".join(f"word{i}" for i in range(80))
    site.set(
        "/plain", f"<html><body><p>{words}</p><script src='/analytics.js'></script></body></html>"
    )
    site.set("/short", "<html><body><p>No openings right now.</p></body></html>")
    site.set("/inline", "<html><body><p>Closed</p><script>var build=3</script></body></html>")
    site.set("/shell-but-static", SHELL)
    ids = [
        await add(client, site.url("/plain")),
        await add(client, site.url("/short")),
        await add(client, site.url("/inline")),
        await add(
            client, site.url("/shell-but-static"), check_method="static"
        ),  # explicit: respected
    ]
    await settle(engine, clock)
    await advance(engine, clock, 130, step=10)
    assert browser.calls == 0
    for bid in ids:
        b = (await client.get(f"/bookmarks/{bid}")).json()
        assert b["check_method"] in ("auto", "static") and b["latest_version_id"]


async def test_detection_only_runs_on_the_first_check(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    browser = ScriptedFetcher(lambda req, n: ok(req, R1))
    engine.fetchers.replace("browser", browser)  # type: ignore[arg-type]
    words = " ".join(f"word{i}" for i in range(80))
    site.set("/p", f"<html><body><p>{words}</p></body></html>")
    bid = await add(client, site.url("/p"))
    await settle(engine, clock)
    site.set("/p", SHELL)  # later the page turns into an empty shell (an outage, a redesign)
    await advance(engine, clock, 70, step=10)
    assert browser.calls == 0  # not silently re-routed: it is a change in a static page
    assert (await client.get(f"/bookmarks/{bid}")).json()["check_method"] == "auto"


async def test_an_explicit_browser_bookmark_uses_the_browser_from_the_start(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/app", SHELL)
    browser = ScriptedFetcher(lambda req, n: ok(req, R1))
    engine.fetchers.replace("browser", browser)  # type: ignore[arg-type]
    bid = await add(client, site.url("/app"), check_method="browser")
    assert engine.scheduler._entries[bid].browser is True
    await settle(engine, clock)
    assert site.hits_for("/app") == [] and browser.calls == 1
    assert (await runs(client, bid))[0]["method"] == "browser"
    assert (await runs(client, bid))[0]["reason"] is None  # chosen, not switched


# -- the screenshot method --------------------------------------------------------------

BASE = [(100, 100, 500, 60, "black"), (100, 400, 600, 20, "gray")]
DASH = "<html><body><h1>Dashboard</h1><p>All systems normal.</p></body></html>"


async def test_screenshot_diff_boxes_the_changed_region_through_the_whole_engine(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, toasts: LogToastBackend
) -> None:
    p0 = page_png(BASE)
    noisy = Image.open(io.BytesIO(p0))
    noisy.putpixel((3, 3), (0, 0, 0))
    p1 = page_png([*BASE, (800, 600, 200, 100, "red")])
    p2 = page_png([*BASE, (800, 600, 200, 100, "red"), (100, 700, 300, 80, "blue")])
    state = {"png": p0}
    shooter = ScriptedFetcher(lambda req, n: ok(req, DASH, png=state["png"]))
    engine.fetchers.replace("screenshot", shooter)  # type: ignore[arg-type]
    bid = await add(client, "https://dash.test/", check_method="screenshot", name="Dashboard")
    await settle(engine, clock)
    first = (await runs(client, bid))[0]
    assert first["outcome"] == "first" and first["method"] == "screenshot"

    await advance(engine, clock, 70, step=10)  # identical pixels
    assert (await runs(client, bid))[0]["outcome"] == "unchanged" and not toasts.shown
    state["png"] = png_bytes(noisy)  # one stray pixel: below min_ratio
    await advance(engine, clock, 70, step=10)
    assert (await runs(client, bid))[0]["outcome"] == "unchanged" and not toasts.shown
    versions = await engine.db.read(
        lambda c: c.execute("SELECT COUNT(*) FROM version").fetchone()[0]
    )
    assert versions == 1  # neither was stored

    state["png"] = p1
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and toasts.shown[0].body.startswith("Visual change: ")
    (change,) = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    assert change["summary"].startswith("Visual change:") and change["changed_blocks"] == 1
    rows = await engine.db.read(
        lambda c: c.execute("SELECT screenshot_hash FROM version ORDER BY id").fetchall()
    )
    assert len(rows) == 2 and all(r["screenshot_hash"] for r in rows)

    # the change's own diff, as the overlay picture
    r = await client.get(
        f"/changes/{change['id']}/render", params={"view": "screenshot", "format": "png"}
    )
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-pagewatch-regions"] == "1" and r.headers["x-pagewatch-identical"] == "false"
    overlay = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert overlay.size == (1366, 900)
    reds = [
        (x, y)
        for x in range(780, 1020)
        for y in (598, 599, 600)
        if overlay.getpixel((x, y)) == (255, 0, 0)
    ]
    assert reds  # a red box edge hugs the changed rectangle
    assert overlay.getpixel((900, 650)) == (
        255,
        0,
        0,
    )  # the page's own red square, untouched inside
    html = (await client.get(f"/changes/{change['id']}/render", params={"view": "screenshot"})).text
    assert "data:image/png;base64," in html and "Visual change:" in html
    j = (await client.get(f"/changes/{change['id']}/render",
                          params={"view": "screenshot", "format": "json"})).json()  # fmt: skip
    assert (
        j["view"] == "screenshot"
        and j["stats"]["regions"] == 1
        and j["stats"]["changed_pixels"] > 0
    )

    # a second visual change: its own diff has one region, the unread diff (last read -> latest) two
    state["png"] = p2
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 2
    ch = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    newest = ch[0]["id"]
    own = await client.get(
        f"/changes/{newest}/render", params={"view": "screenshot", "format": "png"}
    )
    assert own.headers["x-pagewatch-regions"] == "1"
    unread = await client.get(
        f"/bookmarks/{bid}/diff", params={"view": "screenshot", "format": "png"}
    )
    assert unread.headers["x-pagewatch-regions"] == "2"
    # the text views keep working on a screenshot bookmark
    for view in ("text", "highlight", "new", "old"):
        assert (
            await client.get(f"/bookmarks/{bid}/diff", params={"view": view})
        ).status_code == 200
    await client.post(f"/bookmarks/{bid}/read")
    after = await client.get(
        f"/bookmarks/{bid}/diff", params={"view": "screenshot", "format": "png"}
    )
    assert (
        after.headers["x-pagewatch-identical"] == "true"
        and after.headers["x-pagewatch-regions"] == "0"
    )
    assert Image.open(io.BytesIO(after.content)).getpixel((900, 650)) == (
        255,
        0,
        0,
    )  # just the picture


async def test_screenshot_views_are_404_without_a_screenshot_and_png_is_screenshot_only(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/t", "<html><body><p>one</p></body></html>")
    bid = await add(client, site.url("/t"))
    await settle(engine, clock)
    site.set("/t", "<html><body><p>two</p></body></html>")
    await advance(engine, clock, 70, step=10)
    (change,) = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    for url in (f"/changes/{change['id']}/render", f"/bookmarks/{bid}/diff"):
        assert (await client.get(url, params={"view": "screenshot"})).status_code == 404
        assert (await client.get(url, params={"view": "text", "format": "png"})).status_code == 422
    assert (await client.get(f"/bookmarks/{bid}/diff",
                             params={"view": "new", "format": "png"})).status_code == 422  # fmt: skip
    assert (
        await client.get("/changes/999/render", params={"view": "screenshot"})
    ).status_code == 404


async def test_a_clipped_or_viewport_screenshot_is_passed_to_the_fetcher_as_configured(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    shooter = ScriptedFetcher(lambda req, n: ok(req, DASH, png=page_png(BASE, size=(300, 200))))
    engine.fetchers.replace("screenshot", shooter)  # type: ignore[arg-type]
    clip = {"x": 0, "y": 0, "w": 300, "h": 200}
    await add(client, "https://dash.test/", check_method="screenshot",
              fetch={"browser": {"clip": clip, "scroll_count": 2}},
              filter={"screenshot": {"ignore": [{"x": 0, "y": 0, "w": 10, "h": 10}], "min_ratio": 0.05}})  # fmt: skip
    await settle(engine, clock)
    cfg = shooter.requests[0].resolved
    assert cfg.fetch.browser.clip is not None and cfg.fetch.browser.clip.w == 300
    assert cfg.fetch.browser.scroll_count == 2 and cfg.filter.screenshot.min_ratio == 0.05


# -- health and the browser's lifecycle -------------------------------------------------


async def test_health_reports_the_browsers_state(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock
) -> None:
    assert (await client.get("/health")).json()["browser_state"] == "stopped"
    launcher = FakeLauncher()
    engine.browser._launcher = launcher  # type: ignore[assignment]
    await engine.browser.ensure()
    assert (await client.get("/health")).json()["browser_state"] == "running"
    await clock.run_for(700)  # ten idle minutes
    assert (await client.get("/health")).json()["browser_state"] == "stopped"
    assert launcher.browsers[0].closed
    broken = FakeLauncher(fail={"channel": "no edge", "bundled": "no chromium"})
    engine.browser._launcher = broken  # type: ignore[assignment]
    with contextlib.suppress(Exception):
        await engine.browser.ensure()
    assert (await client.get("/health")).json()["browser_state"] == "unavailable"


async def test_the_engine_closes_the_browser_on_stop(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    launcher = FakeLauncher()
    engine.browser._launcher = launcher  # type: ignore[assignment]
    await engine.browser.ensure()
    await engine.stop()
    assert launcher.browsers[0].closed and launcher.closed


# -- the add-bookmark preview -----------------------------------------------------------


async def test_preview_renders_a_javascript_app_in_the_browser_under_auto(
    client: httpx.AsyncClient, engine: Engine, site: FixtureSite
) -> None:
    site.set("/app", SHELL)
    browser = ScriptedFetcher(lambda req, n: ok(req, R1))
    engine.fetchers.replace("browser", browser)  # type: ignore[arg-type]
    r = await client.post("/preview", json={"url": site.url("/app")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["method"] == "browser" and out["js_app"] is True and out["error"] is None
    assert "Latest lotteries" in out["html"] and "rendered in the browser" in out["warnings"]
    assert browser.calls == 1


async def test_preview_says_so_when_the_browser_cannot_be_used(
    client: httpx.AsyncClient, engine: Engine, site: FixtureSite
) -> None:
    site.set("/app", SHELL)
    engine.fetchers.replace(
        "browser",
        ScriptedFetcher(  # type: ignore[arg-type]
            lambda req, n: error(req, FetchErrorKind.BROWSER, "no browser could be started")
        ),
    )
    out = (await client.post("/preview", json={"url": site.url("/app")})).json()
    assert out["method"] == "browser" and out["js_app"] is True and out["error"] is None
    assert any(
        "needs a browser" in w and "no browser could be started" in w for w in out["warnings"]
    )


async def test_preview_with_an_explicit_method_does_not_second_guess_it(
    client: httpx.AsyncClient, engine: Engine, site: FixtureSite
) -> None:
    site.set("/app", SHELL)
    browser = ScriptedFetcher(lambda req, n: ok(req, R1))
    engine.fetchers.replace("browser", browser)  # type: ignore[arg-type]
    out = (
        await client.post("/preview", json={"url": site.url("/app"), "check_method": "static"})
    ).json()
    assert out["method"] == "static" and browser.calls == 0 and out["js_app"] is True
    out = (
        await client.post("/preview", json={"url": site.url("/app"), "check_method": "browser"})
    ).json()
    assert out["method"] == "browser" and "Latest lotteries" in out["html"] and browser.calls == 1


async def test_preview_recognises_documents_feeds_records_and_files(
    client: httpx.AsyncClient, engine: Engine, site: FixtureSite, tmp_path: Any
) -> None:
    site.set(
        "/d.pdf", make_pdf([["Opening hours", "Weekdays 9 to 5"]]), content_type="application/pdf"
    )
    feed = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><link>https://x</link>'
        "<description>d</description><item><title>One</title><guid>1</guid></item></channel></rss>"
    )
    site.set("/f.xml", feed, content_type="application/rss+xml")
    site.set("/r.json", json.dumps([{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]),
             content_type="application/json")  # fmt: skip
    (tmp_path / "note.txt").write_text("a local note")

    pdf = (await client.post("/preview", json={"url": site.url("/d.pdf")})).json()
    assert pdf["kind"] == "pdf" and pdf["blocks"] == 2 and "Weekdays 9 to 5" in pdf["html"]
    fd = (await client.post("/preview", json={"url": site.url("/f.xml")})).json()
    assert fd["kind"] == "feed" and "One" in fd["html"]
    rec = (await client.post("/preview", json={
        "url": site.url("/r.json"), "source_type": "records",
        "fetch": {"records": {"id_field": "id"}}})).json()  # fmt: skip
    assert rec["kind"] == "json" and rec["blocks"] == 2 and "id: 1 | n: a" in rec["html"]
    local = (await client.post("/preview", json={"url": (tmp_path / "note.txt").as_uri()})).json()
    assert local["kind"] == "text" and local["error"] is None and "a local note" in local["html"]
    r = await client.post("/preview", json={"url": site.url("/r.json"), "source_type": "records"})
    assert r.status_code == 422  # records need their configuration here too
