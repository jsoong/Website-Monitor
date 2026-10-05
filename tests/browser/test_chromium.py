"""M4 acceptance against a real Chromium (marker ``browser``; run with ``pytest -m browser``).

The sandbox has Playwright's Chromium but not Microsoft Edge, which is exactly the situation the
fallback exists for. Set PAGEWATCH_TEST_CHROMIUM to point at another Chromium binary.
"""

from __future__ import annotations

import asyncio
import io
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.fetch.browser import LaunchPlan, PlaywrightLauncher
from tests.support.docs import make_pdf
from tests.support.fixture_site import FixtureSite
from tests.support.sim import advance, settle

CHROMIUM = os.environ.get("PAGEWATCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="no Chromium binary available"),
]
SCHED = {"interval_s": 60, "jitter_pct": 0}


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
        "worker_processes": 2, "toast_coalesce_s": 0.0,
        "browser_executable": CHROMIUM, "browser_args": ["--no-sandbox", "--disable-gpu"],
    }  # fmt: skip


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out


def closed_port() -> int:
    """A local port nothing listens on (Chromium refuses a few well-known ports outright)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def until(predicate: Any, limit_s: float = 10.0) -> None:
    """Closing a real browser takes real time, which the fake clock cannot advance."""
    for _ in range(int(limit_s / 0.05)):
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


def js_app(headline: str) -> tuple[str, str]:
    html = (
        "<html><head><title>Lotteries</title></head><body><div id='root'></div>"
        "<script src='/app.js'></script></body></html>"
    )
    script = (
        "setTimeout(function(){document.getElementById('root').innerHTML="
        f"'<h1>{headline}</h1><p>Sunset Terrace is open for applications until November.</p>"
        "<p>Harbor View opens for applications next spring.</p>';}, 100);"
    )
    return html, script


# -- JavaScript pages -------------------------------------------------------------------


async def test_the_js_only_fixture_is_rendered_and_switched_to_the_browser_automatically(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    html, script = js_app("Latest lotteries")
    site.set("/app", html)
    site.set("/app.js", script, content_type="application/javascript")
    bid = await add(client, site.url("/app"), name="Lotteries",
                    fetch={"browser": {"delay_after_load_s": 0.6}})  # fmt: skip
    await settle(engine, clock)

    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["check_method"] == "browser" and b["status"] in ("ok", "new")
    first = (await runs(client, bid))[0]
    assert first["outcome"] == "first" and first["method"] == "browser"
    assert first["reason"] == "auto_browser:app_shell"
    new = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    assert "Latest lotteries" in new and "Sunset Terrace" in new  # the script ran
    assert (await client.get("/health")).json()["browser_state"] == "running"

    site.set(
        "/app.js", js_app("Latest lotteries (updated)")[1], content_type="application/javascript"
    )
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "updated" in toasts.shown[0].body


async def test_the_static_page_that_needs_no_script_never_starts_the_browser(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/p", "<html><body><p>" + "plain words " * 40 + "</p></body></html>")
    await add(client, site.url("/p"))
    await settle(engine, clock)
    assert (await client.get("/health")).json()["browser_state"] == "stopped"


async def test_images_media_and_fonts_are_blocked_except_for_screenshots(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    pic = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(pic, "PNG")
    site.set("/pic.png", pic.getvalue(), content_type="image/png")
    site.set("/pic2.png", pic.getvalue(), content_type="image/png")
    site.set(
        "/a", "<html><body><h1>Gallery</h1><img src='/pic.png' width=40 height=40></body></html>"
    )
    site.set(
        "/b", "<html><body><h1>Gallery</h1><img src='/pic2.png' width=40 height=40></body></html>"
    )
    await add(client, site.url("/a"), check_method="browser")
    await add(client, site.url("/b"), check_method="screenshot")
    await settle(engine, clock)
    assert site.hits_for("/pic.png") == []  # the browser method did not download the image
    assert len(site.hits_for("/pic2.png")) >= 1  # the screenshot method needs it


async def test_scrolling_loads_lazy_content_and_same_origin_iframes_are_inlined(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    other = await FixtureSite().start()  # a second origin (another port)
    try:
        other.set("/ad", "<html><body><p>third party advert text</p></body></html>")
        site.set("/frame", "<html><body><p>text inside the same-origin frame</p></body></html>")
        site.set(
            "/lazy",
            "<html><body style='margin:0'><div style='height:3000px'>tall page</div>"
            "<div id='more'></div>"
            "<iframe src='/frame'></iframe>"
            f"<iframe src='{other.url('/ad')}'></iframe>"
            "<script>window.addEventListener('scroll',function(){"
            "document.getElementById('more').textContent='lazy content appeared after scroll';});"
            "</script></body></html>",
        )
        plain = await add(client, site.url("/lazy"), check_method="browser", name="no scroll")
        scrolled = await add(client, site.url("/lazy"), check_method="browser", name="scroll",
                             fetch={"browser": {"scroll_count": 2}})  # fmt: skip
        await settle(engine, clock)
        no = (await client.get(f"/bookmarks/{plain}/diff", params={"view": "new"})).text
        yes = (await client.get(f"/bookmarks/{scrolled}/diff", params={"view": "new"})).text
        assert "lazy content appeared" not in no and "lazy content appeared" in yes
        for page in (no, yes):
            assert "text inside the same-origin frame" in page  # inlined into the DOM
            assert "third party advert text" not in page  # a cross-origin frame stays out
    finally:
        await other.stop()


async def test_keys_and_mouse_input_reach_the_page(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set(
        "/keys",
        "<html><body><p id='log'>idle</p><script>"
        "document.addEventListener('keydown',function(e){document.getElementById('log').textContent='key '+e.key;});"
        "</script></body></html>",
    )
    bid = await add(client, site.url("/keys"), check_method="browser",
                    fetch={"browser": {"keys": ["End"], "mouse_moves": 2}})  # fmt: skip
    await settle(engine, clock)
    assert "key End" in (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text


async def test_http_errors_and_refused_connections_through_the_browser(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/busy", "<html><body>try later</body></html>", status=503,
             headers={"Retry-After": "90"})  # fmt: skip
    busy = await add(client, site.url("/busy"), check_method="browser")
    dead = await add(client, f"http://127.0.0.1:{closed_port()}/never", check_method="browser")
    await settle(engine, clock)
    assert (await runs(client, busy))[0]["reason"] == "http_503"
    assert (await runs(client, dead))[0]["reason"] == "connection"


async def test_a_file_download_url_is_a_clear_browser_error(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/doc.pdf", make_pdf([["x"]]), content_type="application/pdf",
             headers={"Content-Disposition": "attachment; filename=doc.pdf"})  # fmt: skip
    bid = await add(client, site.url("/doc.pdf"), check_method="browser")
    await settle(engine, clock)
    last = (await runs(client, bid))[0]
    assert last["outcome"] == "error" and last["reason"] == "browser"


# -- screenshots ------------------------------------------------------------------------

VISUAL = """<html><head><style>
body{margin:0;background:#fff;font-family:sans-serif}
.box{position:absolute;background:#222}
</style></head><body>
<div class='box' style='left:100px;top:100px;width:500px;height:60px'></div>
<div class='box' style='left:100px;top:400px;width:600px;height:20px;background:#888'></div>
%s
<div style='position:absolute;left:0;top:1000px;width:10px;height:10px'></div>
</body></html>"""
NEW_BOX = (
    "<div class='box' style='left:800px;top:600px;width:200px;height:100px;background:#d00'></div>"
)


async def test_screenshot_diff_boxes_the_changed_region_on_the_visual_fixture(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/visual", VISUAL % "")
    bid = await add(client, site.url("/visual"), check_method="screenshot", name="Visual")
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["method"] == "screenshot"
    await advance(engine, clock, 130, step=10)  # a real page renders the same twice
    assert not toasts.shown

    site.set("/visual", VISUAL % NEW_BOX)
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and toasts.shown[0].body.startswith("Visual change:")
    (change,) = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    r = await client.get(
        f"/changes/{change['id']}/render", params={"view": "screenshot", "format": "png"}
    )
    assert r.status_code == 200 and r.headers["x-pagewatch-regions"] == "1"
    overlay = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert overlay.width == 1366  # the fixed viewport width, device scale 1
    red = [
        (x, y)
        for x in range(0, overlay.width, 2)
        for y in range(0, min(overlay.height, 900), 2)
        if overlay.getpixel((x, y)) == (255, 0, 0)
    ]
    xs, ys = [p[0] for p in red], [p[1] for p in red]
    assert min(xs) <= 800 and max(xs) >= 1000 and min(ys) <= 600 and max(ys) >= 700
    assert min(xs) >= 780 and max(xs) <= 1020 and min(ys) >= 580 and max(ys) <= 720  # only there


async def test_animations_are_disabled_so_a_moving_element_is_not_a_change(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set(
        "/anim",
        "<html><head><style>@keyframes slide{from{left:0}to{left:800px}}"
        ".m{position:absolute;top:100px;width:100px;height:100px;background:#06c;"
        "animation:slide 1.3s linear infinite}</style></head><body>"
        "<div class='m'></div><p>Static caption</p></body></html>",
    )
    bid = await add(client, site.url("/anim"), check_method="screenshot")
    await settle(engine, clock)
    await advance(engine, clock, 300, step=20)  # five more captures at arbitrary animation phases
    assert not toasts.shown
    assert {r["outcome"] for r in await runs(client, bid)} <= {"first", "unchanged"}


async def test_clip_and_ignore_rectangles_apply_to_real_screenshots(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/visual", VISUAL % "")
    bid = await add(
        client, site.url("/visual"), check_method="screenshot",
        fetch={"browser": {"clip": {"x": 0, "y": 0, "w": 1366, "h": 500}}},
        filter={"screenshot": {"ignore": [{"x": 780, "y": 580, "w": 260, "h": 160}]}},
    )  # fmt: skip
    await settle(engine, clock)
    site.set("/visual", VISUAL % NEW_BOX)  # y=600..700 is outside the 500 px clip and also ignored
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown
    row = await engine.db.read(
        lambda c: c.execute("SELECT screenshot_hash FROM bookmark b JOIN version v "
                            "ON v.id=b.latest_version_id WHERE b.id=?", (bid,)).fetchone()
    )  # fmt: skip
    from pagewatch.engine.store.blobs import BlobStore

    shot = Image.open(io.BytesIO(BlobStore(engine.data_dir.blobs_dir).get(row["screenshot_hash"])))
    assert shot.size == (1366, 500)


# -- the browser's lifecycle ------------------------------------------------------------


async def test_the_browser_closes_after_ten_idle_minutes_and_comes_back_on_demand(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/app", js_app("A")[0])
    site.set("/app.js", js_app("A")[1], content_type="application/javascript")
    bid = await add(client, site.url("/app"), check_method="browser",
                    schedule={"mode": "manual"}, fetch={"browser": {"delay_after_load_s": 0.4}})  # fmt: skip
    await settle(engine, clock)
    assert (await client.get("/health")).json()["browser_state"] == "running"
    assert engine.browser.stats.launches == 1
    await clock.run_for(9 * 60)
    assert (await client.get("/health")).json()["browser_state"] == "running"
    await clock.run_for(2 * 60)

    async def stopped() -> bool:
        return bool((await client.get("/health")).json()["browser_state"] == "stopped")

    await until(stopped)
    assert engine.browser.stats.idle_closes == 1
    r = await client.post(f"/bookmarks/{bid}/check", params={"force": "true"})
    assert r.status_code in (200, 202)
    await settle(engine, clock)
    assert engine.browser.stats.launches == 2
    assert (await client.get("/health")).json()["browser_state"] == "running"


class BundledStandIn(PlaywrightLauncher):
    """What `playwright install chromium` provides, here: the sandbox's Chromium binary."""

    async def __call__(self, plan: LaunchPlan) -> Any:
        if plan.source == "bundled":
            plan = LaunchPlan("executable", CHROMIUM, plan.args)
        return await super().__call__(plan)


@pytest.fixture
async def edge_engine(
    data_dir: Any, clock: FakeClock, settings_overrides: dict[str, Any], toasts: LogToastBackend
) -> AsyncIterator[Engine]:
    """An engine configured like a Windows machine whose Edge will not launch."""
    settings_overrides = {**settings_overrides, "browser_executable": None,
                          "browser_channel": "msedge"}  # fmt: skip
    eng = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
                 toast_backend=toasts, browser_launcher=BundledStandIn())  # fmt: skip
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def test_browser_checks_fall_back_to_the_bundled_chromium_when_edge_will_not_launch(
    edge_engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/app", js_app("A")[0])
    site.set("/app.js", js_app("A")[1], content_type="application/javascript")
    from pagewatch.engine.fetch.base import FetchRequest
    from pagewatch.engine.fetch.select import BROWSER

    resolved = _resolved(edge_engine)
    res = await edge_engine.fetchers.get(BROWSER).fetch(
        FetchRequest(site.url("/app"), resolved, edge_engine.settings, force=True)
    )
    assert res.error is None and "root" in res.body.decode()
    stats = edge_engine.browser.stats
    assert stats.fallbacks == 1 and stats.launches == 1
    assert stats.plans == ["bundled:chromium"]  # Edge failed (not installed here), then bundled
    assert edge_engine.browser.state == "running"


def _resolved(engine: Engine) -> Any:
    from pagewatch.engine.config import Resolved
    from pagewatch.models import (
        ActionsConfig,
        FetchConfig,
        FilterConfig,
        GateConfig,
        ScheduleConfig,
    )

    return Resolved(
        schedule=ScheduleConfig(), fetch=FetchConfig(browser={"delay_after_load_s": 0.4}),  # type: ignore[arg-type]
        filter=FilterConfig(), gate=GateConfig(), actions=ActionsConfig(), overrides={},
    )  # fmt: skip
