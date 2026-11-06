"""The viewer's place in the window (below or right, or the whole window), the Theme, and what the
viewer shows by default: remote content on, a dark empty page, the page's own base address."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter

from pagewatch.ui import theme
from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.main_window import DOCK_BOTTOM, DOCK_RIGHT, MainWindow
from pagewatch.ui.windows.viewer import (
    HtmlView,
    blank_document,
    document_base_url,
)
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article

K = Qt.Key


@pytest.fixture
def settings_home(tmp_path: Path, qapp: QApplication) -> Iterator[Path]:
    """Keep the window's remembered layout out of the real user settings."""
    fmt, scope = QSettings.Format.NativeFormat, QSettings.Scope.UserScope
    home = tmp_path / "qsettings"
    home.mkdir()
    QSettings.setPath(fmt, scope, str(home))
    yield home
    QSettings("PageWatch", "PageWatch").clear()
    QSettings.setPath(fmt, scope, "")


@pytest.fixture
def open_window(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> Iterator[Callable[..., MainWindow]]:
    windows: list[MainWindow] = []

    def factory(**kw: Any) -> MainWindow:
        client = ApiClient(eng.base, eng.engine.token)
        w = MainWindow(client, **kw)
        w.show()
        w.activateWindow()
        QTest.qWaitForWindowActive(w)
        QApplication.processEvents()
        windows.append(w)
        return w

    yield factory
    for w in windows:
        w.close()


def settle() -> None:
    for _ in range(5):
        QApplication.processEvents()


def children(splitter: QSplitter) -> list[Any]:
    return [splitter.widget(i) for i in range(splitter.count())]


def covers(outer: Any, inner: Any, slack: int = 3) -> bool:
    """``inner`` fills ``outer`` (both are laid out: the window is shown)."""
    return inner.width() >= outer.width() - slack and inner.height() >= outer.height() - slack


# -- the default: viewer below, full width ----------------------------------------------------


def test_the_viewer_is_docked_below_the_folders_and_list_across_the_full_width(
    open_window: Callable[..., MainWindow],
) -> None:
    w = open_window()
    assert w.viewer_dock == DOCK_BOTTOM and not w.viewer_maximized
    assert w.splitter.orientation() == Qt.Orientation.Vertical
    assert children(w.splitter) == [w.list_split, w.viewer]
    assert w.list_split.orientation() == Qt.Orientation.Horizontal
    assert children(w.list_split) == [w.tree, w.middle]  # folders | bookmark list, side by side
    assert w.viewer.width() >= w.splitter.width() - 3  # not squeezed into a column
    assert w.viewer.y() > w.list_split.y() and w.tree.x() < w.middle.x()
    assert (w.viewer.btn_dock.text(), w.viewer.btn_max.text()) == ("Dock right", "Expand")


def test_dock_right_is_the_classic_three_columns_and_back_again(
    open_window: Callable[..., MainWindow],
) -> None:
    w = open_window()
    w.viewer.btn_dock.click()  # the button in the viewer's header
    settle()
    assert w.viewer_dock == DOCK_RIGHT
    assert w.splitter.orientation() == Qt.Orientation.Horizontal
    assert children(w.splitter) == [w.tree, w.middle, w.viewer]
    assert w.list_split.parent() is None and not w.list_split.isVisible()  # idle, not a window
    assert w.tree.x() < w.middle.x() < w.viewer.x()
    assert w.viewer.height() >= w.splitter.height() - 3
    assert w.viewer.btn_dock.text() == "Dock bottom" and w.act_dock.text() == "Dock viewer bottom"
    assert all(p.isVisible() for p in (w.tree, w.middle, w.viewer))

    w.act_dock.trigger()  # the menu / toolbar action
    settle()
    assert w.viewer_dock == DOCK_BOTTOM and children(w.splitter) == [w.list_split, w.viewer]
    assert children(w.list_split) == [w.tree, w.middle]
    assert w.viewer.width() >= w.splitter.width() - 3 and w.tree.isVisible()
    assert w.viewer.btn_dock.text() == "Dock right"


def test_the_dock_shortcut_works_from_the_keyboard(open_window: Callable[..., MainWindow]) -> None:
    w = open_window()
    assert QKeySequence("Ctrl+Shift+D") in w.act_dock.shortcuts()
    QTest.keyClick(
        w, K.Key_D, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    assert w.viewer_dock == DOCK_RIGHT
    QTest.keyClick(
        w, K.Key_D, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    assert w.viewer_dock == DOCK_BOTTOM


def test_the_viewer_keeps_its_web_page_when_the_layout_changes(
    open_window: Callable[..., MainWindow],
) -> None:
    w = open_window()
    web = w.viewer.web
    w.viewer.web.set_document("<html><body><p>kept</p></body></html>")
    for _ in range(2):
        w.set_viewer_dock(DOCK_RIGHT if w.viewer_dock == DOCK_BOTTOM else DOCK_BOTTOM)
    assert w.viewer.web is web and web.last_html.endswith("</html>") and "kept" in web.last_html


# -- expand: the viewer has the whole window ----------------------------------------------------


@pytest.mark.parametrize("dock", [DOCK_BOTTOM, DOCK_RIGHT])
def test_expand_gives_the_viewer_the_whole_window_and_restore_brings_the_panes_back(
    open_window: Callable[..., MainWindow], dock: str
) -> None:
    w = open_window()
    w.set_viewer_dock(dock)
    settle()
    before = (w.tree.width(), w.middle.width(), w.viewer.width(), w.viewer.height())
    w.viewer.btn_max.click()
    settle()
    assert w.viewer_maximized and w.viewer.btn_max.text() == "Restore"
    assert w.tree.isHidden() or w.list_split.isHidden()
    assert not w.tree.isVisible() and not w.middle.isVisible()
    assert covers(w.splitter, w.viewer) and w.viewer.x() <= 1 and w.viewer.y() <= 1
    assert w.act_restore.isEnabled() and w.act_maximize.text() == "Restore viewer"

    w.viewer.btn_max.click()  # the same button again
    settle()
    assert not w.viewer_maximized and w.viewer.btn_max.text() == "Expand"
    assert w.tree.isVisible() and w.middle.isVisible() and not w.act_restore.isEnabled()
    after = (w.tree.width(), w.middle.width(), w.viewer.width(), w.viewer.height())
    assert all(abs(a - b) <= 3 for a, b in zip(before, after, strict=True)), (before, after)


def test_f11_ctrl_m_and_escape_drive_the_expanded_viewer(
    open_window: Callable[..., MainWindow],
) -> None:
    w = open_window()
    assert QKeySequence("F11") in w.act_maximize.shortcuts()
    assert QKeySequence("Ctrl+M") in w.act_maximize.shortcuts()
    assert QKeySequence("Esc") in w.act_restore.shortcuts()
    QTest.keyClick(w, K.Key_F11)
    assert w.viewer_maximized
    QTest.keyClick(w, K.Key_F11)
    assert not w.viewer_maximized
    QTest.keyClick(w, K.Key_M, Qt.KeyboardModifier.ControlModifier)
    assert w.viewer_maximized
    QTest.keyClick(w, K.Key_Escape)  # Esc restores
    assert not w.viewer_maximized and w.tree.isVisible()
    QTest.keyClick(w, K.Key_Escape)  # and does nothing when there is nothing to restore
    assert not w.viewer_maximized and not w.act_restore.isEnabled()


def test_an_expanded_viewer_stays_expanded_when_the_dock_changes(
    open_window: Callable[..., MainWindow],
) -> None:
    w = open_window()
    w.set_viewer_maximized(True)
    for where in (DOCK_RIGHT, DOCK_BOTTOM, DOCK_RIGHT):
        w.set_viewer_dock(where)
        settle()
        assert w.viewer_maximized and not w.tree.isVisible() and not w.middle.isVisible()
        assert covers(w.splitter, w.viewer)
    w.set_viewer_maximized(False)
    settle()
    assert w.tree.isVisible() and w.middle.isVisible() and w.viewer.x() > w.middle.x()


def test_reviewing_still_works_with_the_list_hidden(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[..., MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    for i in range(2):
        eng.site.set(f"/p{i}", article(f"story {i} alpha", title=f"P{i}"))
        api_client.create_bookmark({"url": eng.site.url(f"/p{i}"), "name": f"P{i}"})
    w = open_window()
    pump_until(lambda: w.model.rowCount() == 2, what="list")
    w.set_viewer_maximized(True)
    w.next_unread()  # N / Space act on the hidden list
    w.table.selectRow(1)
    pump_until(lambda: w.viewer.bookmark_id is not None, what="viewer follows the selection")
    assert w.viewer_maximized and w.viewer.title.text() in ("P0", "P1")


# -- remembered across restarts -------------------------------------------------------------------


def test_a_fresh_install_starts_docked_below_and_not_expanded(
    open_window: Callable[..., MainWindow], settings_home: Path
) -> None:
    w = open_window(persist=True)
    assert w.viewer_dock == DOCK_BOTTOM and not w.viewer_maximized


def test_the_dock_and_the_expanded_state_survive_a_restart(
    open_window: Callable[..., MainWindow], settings_home: Path
) -> None:
    first = open_window(persist=True)
    first.set_viewer_dock(DOCK_RIGHT)
    first.set_viewer_maximized(True)
    settle()
    first.close()

    second = open_window(persist=True)
    settle()
    assert second.viewer_dock == DOCK_RIGHT and second.viewer_maximized
    assert children(second.splitter) == [second.tree, second.middle, second.viewer]
    assert not second.tree.isVisible() and covers(second.splitter, second.viewer)
    assert (
        second.viewer.btn_max.text() == "Restore" and second.viewer.btn_dock.text() == "Dock bottom"
    )
    second.set_viewer_maximized(False)
    settle()
    assert second.tree.isVisible() and second.middle.isVisible()
    second.close()

    third = open_window(persist=True)
    assert third.viewer_dock == DOCK_RIGHT and not third.viewer_maximized  # the last choice


def test_pane_sizes_come_back_for_each_layout(
    open_window: Callable[..., MainWindow], settings_home: Path
) -> None:
    def share(w: MainWindow) -> float:
        return w.tree.width() / w.list_split.width()  # the tree's share of the top pane

    first = open_window(persist=True)
    first.list_split.setSizes([150, 900])
    settle()
    wanted = share(first)
    assert 0.1 < wanted < 0.25  # not the default [240, 1100] split
    first.set_viewer_maximized(True)  # closing while expanded must not save "no width"
    first.close()
    second = open_window(persist=True)  # (a different window size: compare shares, not pixels)
    second.set_viewer_maximized(False)
    settle()
    assert abs(share(second) - wanted) < 0.02


def test_a_nonsense_stored_dock_falls_back_to_the_default(
    open_window: Callable[..., MainWindow], settings_home: Path
) -> None:
    QSettings("PageWatch", "PageWatch").setValue("viewer_dock", "diagonal")
    assert open_window(persist=True).viewer_dock == DOCK_BOTTOM


# -- theme ----------------------------------------------------------------------------------------


@pytest.fixture
def restore_theme(qapp: QApplication) -> Iterator[None]:
    yield
    theme.apply("light")  # what the rest of the suite had


def test_dark_is_the_default_and_uses_the_fusion_style(
    qapp: QApplication, restore_theme: None
) -> None:
    assert theme.DEFAULT_THEME == "dark"
    assert theme.apply() == "dark"
    assert qapp.style().objectName().lower() == "fusion"  # the OS style would ignore the scheme
    assert theme.is_dark() and theme.background().name() == "#1e1e1e"
    assert theme.apply("light") == "light" and not theme.is_dark()
    assert theme.background().name() == "#ffffff"
    assert theme.apply("sepia") == "dark"  # unknown: the default
    assert theme.is_dark()


def test_system_follows_the_platform_again_after_a_forced_scheme(
    qapp: QApplication, restore_theme: None
) -> None:
    theme.apply("dark")
    assert theme.is_dark()
    theme.apply("system")  # nothing forced: the platform decides (light, offscreen)
    assert not theme.is_dark()


def test_the_theme_is_a_setting_and_saving_it_restyles_the_window(
    api_client: ApiClient, open_window: Callable[..., MainWindow], qapp: QApplication,
    restore_theme: None, pump_until: Callable[..., None],
) -> None:  # fmt: skip
    from pagewatch.ui.windows.settings import SettingsDialog

    assert api_client.settings().theme == "dark"
    d = SettingsDialog(api_client, api_client.settings())
    combo = d.w["theme"]
    assert [combo.itemData(i) for i in range(combo.count())] == ["dark", "light", "system"]  # type: ignore[attr-defined]
    assert combo.currentData() == "dark"  # type: ignore[attr-defined]
    combo.setCurrentIndex(combo.findData("light"))  # type: ignore[attr-defined]
    assert d.build_patch() == {"theme": "light"}
    saved: list[bool] = []
    d.saved.connect(lambda: saved.append(True))
    d.save(close=False)
    pump_until(lambda: bool(saved), what="saved")
    assert api_client.settings().theme == "light" and d.settings.theme == "light"

    w = open_window()
    theme.apply("dark")
    w.apply_theme(d.settings.theme)  # what the window does when the dialog saves
    assert not theme.is_dark()
    assert w.viewer.web.page().backgroundColor().name() == "#ffffff"
    w.apply_theme("dark")
    assert w.viewer.web.page().backgroundColor().name() == "#1e1e1e"
    d.close()


# -- the web view ---------------------------------------------------------------------------------


def test_remote_images_are_on_by_default_and_the_page_gets_its_base_address(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[..., MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    eng.site.set("/rich", article("story alpha", title="Rich"))
    b = api_client.create_bookmark({"url": eng.site.url("/rich"), "name": "Rich"})
    eng.settle()  # the first check stores the page the viewer will show
    w = open_window()
    assert w.viewer.images.isChecked()
    w.viewer.show_bookmark(b.id, "Rich")
    pump_until(lambda: "<base href=" in w.viewer.web.last_html, what="a remote-enabled document")
    assert f'<base href="{eng.site.url("/rich")}"' in w.viewer.web.last_html
    assert "img-src * data: blob: http: https:" in w.viewer.web.last_html

    w.viewer.images.setChecked(False)  # the switch still works
    pump_until(lambda: "<base href=" not in w.viewer.web.last_html, what="a text-only document")
    assert "img-src data:;" in w.viewer.web.last_html


def test_the_base_address_comes_from_the_document_and_only_if_it_is_http(qapp: Any) -> None:
    doc = (
        '<html><head><meta charset="utf-8"><base href="https://x.test/a?b=1&amp;c=2"></head></html>'
    )
    assert document_base_url(doc) == QUrl("https://x.test/a?b=1&c=2")  # the entity is undone
    assert document_base_url("<html><body>no base</body></html>") == QUrl("about:blank")
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "ftp://x.test/"):
        assert document_base_url(f'<html><head><base href="{bad}"></head>') == QUrl("about:blank")


def test_an_empty_view_is_the_themes_background_not_white(
    qapp: QApplication, restore_theme: None, pump_until: Callable[..., None]
) -> None:
    theme.apply("dark")
    assert "background:#1e1e1e" in blank_document()
    view = HtmlView()
    assert view.page().backgroundColor().name() == "#1e1e1e"
    loaded: list[bool] = []
    view.document_loaded.connect(lambda: loaded.append(True))
    view.set_document("")
    assert view.last_html == ""  # callers still see "no document"
    pump_until(lambda: bool(loaded), what="blank page")
    colour: list[str] = []
    view.page().runJavaScript("getComputedStyle(document.body).backgroundColor", colour.append)
    pump_until(lambda: bool(colour), what="computed colour")
    assert colour[0] == "rgb(30, 30, 30)"
    theme.apply("light")
    view.sync_background()
    assert (
        view.page().backgroundColor().name() == "#ffffff"
        and "background:#ffffff" in blank_document()
    )
    view.deleteLater()


def test_a_view_follows_the_scheme_while_it_lives_and_is_not_called_after_it_is_gone(
    qapp: QApplication, restore_theme: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows Qt emits ``colorSchemeChanged`` when the Theme changes. A closed dialog's
    preview must not be called then: a lambda connection outlives its widget."""
    import gc
    import sys

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QGuiApplication

    theme.apply("light")
    view = HtmlView()
    assert view.page().backgroundColor().name() == "#ffffff"
    theme.apply("dark")
    QGuiApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    assert view.page().backgroundColor().name() == "#1e1e1e"  # followed without being asked

    errors: list[Any] = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    view.deleteLater()
    del view
    for _ in range(5):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        gc.collect()
    QGuiApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
    QApplication.processEvents()
    assert errors == []
