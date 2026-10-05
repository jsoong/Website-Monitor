"""The viewer's Screenshot diff tab (M4 replaces the M3 placeholder)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.viewer import T_SHOT
from tests.support.docs import page_png
from tests.support.engine_thread import EngineThread
from tests.support.fakes import ScriptedFetcher, ok
from tests.support.fixture_site import article
from tests.ui.test_main_window import open_window, rendered  # noqa: F401  (fixture + helper)

K = Qt.Key
BASE = [(100, 100, 500, 60, "black"), (100, 400, 600, 20, "gray")]
DASH = "<html><body><h1>Dashboard</h1><p>All systems normal.</p></body></html>"
SCHED = {"interval_s": 60, "jitter_pct": 0}


def red_box(img: QImage) -> tuple[int, int, int, int] | None:
    """Bounding box of the reddish overlay pixels (the picture is scaled with smoothing, so the
    pure red of the outline is blended with its neighbours)."""
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() > 180 and c.green() < 110 and c.blue() < 110:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def test_the_screenshot_tab_shows_the_overlay_for_unread_changes_and_for_each_alert(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    p0 = page_png(BASE)
    p1 = page_png([*BASE, (800, 600, 200, 100, "blue")])
    p2 = page_png([*BASE, (800, 600, 200, 100, "blue"), (100, 700, 300, 80, "green")])
    state = {"png": p0}
    eng.engine.fetchers.replace(  # type: ignore[arg-type]
        "screenshot", ScriptedFetcher(lambda req, n: ok(req, DASH, png=state["png"]))
    )
    api_client.create_bookmark(
        {"url": "https://dash.test/", "name": "Dashboard", "check_method": "screenshot",
         "schedule": SCHED}
    )  # fmt: skip
    eng.settle()
    state["png"] = p1
    eng.advance(70)
    assert api_client.counts().unread == 1

    w = open_window()
    pump_until(lambda: w.model.rowCount() == 1, what="list loaded")
    QTest.keyClick(w, K.Key_N)  # the one unread bookmark
    # Ctrl+5: the Screenshot diff tab, by keyboard like the other tabs
    rendered(w, pump_until, lambda: QTest.keyClick(w, K.Key_5, Qt.KeyboardModifier.ControlModifier),
             T_SHOT)  # fmt: skip
    v = w.viewer
    assert v.current_tab() == T_SHOT and v.stack.currentWidget() is v.shot
    assert v.shot_caption.text().startswith("1 changed region (boxed in red)")
    pm = v.shot_image.pixmap()
    assert pm is not None and not pm.isNull()
    assert pm.width() <= v.shot_scroll.viewport().width()  # fitted to the pane, never wider
    scale = pm.width() / 1366
    box = red_box(pm.toImage())
    assert box is not None
    x0, y0, x1, y1 = box  # the red box hugs the changed rectangle (800..1000 x 600..700)
    assert abs(x0 - 800 * scale) < 12 * scale + 3 and abs(x1 - 1000 * scale) < 12 * scale + 3
    assert abs(y0 - 600 * scale) < 12 * scale + 3 and abs(y1 - 700 * scale) < 12 * scale + 3

    # a second visual change: unread = both regions; the newest alert on its own = one
    state["png"] = p2
    eng.advance(70)
    pump_until(lambda: v.history.count() == 3, what="history list")
    rendered(w, pump_until, lambda: v.reload(), T_SHOT)
    pump_until(lambda: "2 changed regions" in v.shot_caption.text(), what="unread diff")
    rendered(w, pump_until, lambda: v.history.setCurrentIndex(1), T_SHOT)  # newest alert
    pump_until(lambda: v.shot_caption.text().startswith("1 changed region"), what="alert diff")

    # reading it all leaves nothing unread: the caption says so
    v.history.setCurrentIndex(0)
    api_client.mark_read(w.current_summary().id)  # type: ignore[union-attr]
    rendered(w, pump_until, lambda: v.reload(), T_SHOT)
    pump_until(lambda: v.shot_caption.text().startswith("Nothing unread"), what="read state")


def test_a_bookmark_without_screenshots_explains_how_to_get_them(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    eng.site.set("/t", article("one", title="Text page"))
    api_client.create_bookmark({"url": eng.site.url("/t"), "name": "Text", "schedule": SCHED})
    eng.settle()
    eng.site.set("/t", article("one", "two", title="Text page"))
    eng.advance(70)
    w = open_window()
    pump_until(lambda: w.model.rowCount() == 1, what="list loaded")
    QTest.keyClick(w, K.Key_N)
    QTest.keyClick(w, K.Key_5, Qt.KeyboardModifier.ControlModifier)
    pump_until(lambda: "No screenshot has been stored" in w.viewer.shot_caption.text(),
               what="explanation")  # fmt: skip
    assert "Screenshot" in w.viewer.shot_caption.text()
    assert w.viewer.shot_image.pixmap() is None or w.viewer.shot_image.pixmap().isNull()
