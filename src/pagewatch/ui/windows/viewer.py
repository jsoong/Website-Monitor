"""The change viewer: one shared web view behind a row of tabs.

Highlighted and Text diff show *last read -> latest* (everything unread) by default; the
history list steps through each alert's own diff. All HTML comes from the engine, already
sanitised; the view additionally refuses to navigate and sends link clicks to the OS browser.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from collections.abc import Callable
from importlib import resources
from typing import Any

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPixmap, QResizeEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import ChangeOut, CheckRunOut, RenderOut
from pagewatch.ui import theme
from pagewatch.ui.client import ApiClient, ApiError, ScreenshotDiff
from pagewatch.ui.models import fmt_time
from pagewatch.ui.workers import run_async

TABS = ["Highlighted", "Text diff", "New", "Old", "Screenshot diff", "Check log"]
T_HIGHLIGHT, T_TEXT, T_NEW, T_OLD, T_SHOT, T_LOG = range(6)
DELETION_MODES = [
    ("Struck through in place", "inline"),
    ("Side panel", "panel"),
    ("Hidden", "none"),
]


#: The bridge lives in an isolated world: the (sanitised, script-free) page cannot reach it.
BRIDGE_WORLD = int(QWebEngineScript.ScriptWorldId.ApplicationWorld.value)
#: Tabs whose HTML carries DOM paths, so the filter assistant can be used on them
ASSIST_TABS = (T_HIGHLIGHT, T_NEW)


def _ui_css() -> str:
    return resources.files("pagewatch.ui").joinpath("web/diff.css").read_text(encoding="utf-8")


def _bridge_js() -> str:
    return resources.files("pagewatch.ui").joinpath("web/bridge.js").read_text(encoding="utf-8")


def _qwebchannel_js() -> str | None:
    """Qt's own ``qwebchannel.js`` (a resource of the QtWebChannel module), or ``None``."""
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        return bytes(f.readAll().data()).decode("utf-8")
    finally:
        f.close()


class FilterBridge(QObject):
    """What ``bridge.js`` calls: the selection (and its DOM path) the user made in the page."""

    selection = Signal(dict)

    @Slot(str)
    def selected(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if not isinstance(data, dict) or not isinstance(data.get("path"), str):
            return
        end = data.get("end_path")
        self.selection.emit(
            {
                "via": str(data.get("via", "")),
                "text": str(data.get("text", ""))[:4000],
                "path": data["path"],
                "end_path": end if isinstance(end, str) else None,
            }
        )


class _Page(QWebEnginePage):
    """Never navigates away from the rendered change: links open in the default browser."""

    def acceptNavigationRequest(
        self, url: QUrl, type_: QWebEnginePage.NavigationType, is_main_frame: bool
    ) -> bool:
        if type_ == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return True


_BASE_HREF = re.compile(r'<base\s+href="([^"]+)"', re.IGNORECASE)
_ABOUT_BLANK = "about:blank"


def document_base_url(html: str) -> QUrl:
    """The page address the engine put in the document's ``<base href>`` (it only does so when
    remote content is on), or ``about:blank``. Handing it to ``setHtml`` makes relative images,
    stylesheets and fonts resolve against the site and puts the document in the site's origin, so
    web fonts load as they would in a browser. Scripts stay blocked by the document's CSP."""
    m = _BASE_HREF.search(html[:4096])  # it sits right after the CSP in <head>
    if m:
        url = QUrl(htmllib.unescape(m.group(1)))
        if url.isValid() and url.scheme() in ("http", "https"):
            return url
    return QUrl(_ABOUT_BLANK)


def blank_document() -> str:
    """What an empty view shows: the theme's background, so there is no white flash."""
    colour = theme.background().name()
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="color-scheme" content="light dark">'
        f'</head><body style="background:{colour}"></body></html>'
    )


class HtmlView(QWebEngineView):
    document_loaded = Signal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setPage(_Page(self))
        self.loadFinished.connect(lambda _ok: self.document_loaded.emit())
        self.last_html = ""
        self.sync_background()
        # A bound method of this QObject, not a lambda: Qt drops the connection when the view is
        # destroyed (a closed dialog's preview), a lambda would outlive it and fail on the next
        # change of the Theme.
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._scheme_changed)
        self.bridge = FilterBridge(self)
        self.bridge_ready = self._install_bridge()

    def _install_bridge(self) -> bool:
        """Connect the page to ``bridge.js`` over QWebChannel, in an isolated world.

        Qt injects ``QWebEngineScript``s into the first document only: a second ``setHtml`` into
        the same page starts a fresh script context that never sees them (probed with Qt
        6.11). So the scripts are run again by hand after every load."""
        qwc = _qwebchannel_js()
        if qwc is None:
            return False
        page = self.page()
        self._channel = QWebChannel(page)
        self._channel.registerObject("pagewatch", self.bridge)
        page.setWebChannel(self._channel, BRIDGE_WORLD)
        self._bridge_sources = (qwc, _bridge_js())
        self.loadFinished.connect(self._inject_bridge)
        return True

    def _inject_bridge(self, ok: bool) -> None:
        if ok:
            for source in self._bridge_sources:
                self.page().runJavaScript(source, BRIDGE_WORLD)

    def request_selection(self, done: Callable[[bool], None]) -> None:
        """Ask the page to report its current selection as if the user had Alt+selected it;
        ``done(False)`` when there is nothing the assistant can use (no selection, wrong tab)."""
        self.page().runJavaScript(
            "window.__pwReport ? window.__pwReport('shortcut') : false",
            BRIDGE_WORLD,
            lambda ok: done(bool(ok)),
        )

    def _scheme_changed(self, _scheme: Qt.ColorScheme) -> None:
        self.sync_background()

    def sync_background(self) -> None:
        """Paint the theme's background behind the page (dark: #1e1e1e), not white."""
        self.page().setBackgroundColor(theme.background())

    def set_document(self, html: str) -> None:
        self.last_html = html
        self.setHtml(html or blank_document(), document_base_url(html))


def style_document(html: str, deletions: str) -> str:
    """Apply the deletions toggle (a class on <body>) and the UI's extra stylesheet."""
    html = html.replace("pw-del-inline", f"pw-del-{deletions}", 1)
    return html.replace("</style>", _ui_css() + "</style>", 1)


class ViewerPanel(QWidget):
    #: (tab index, view produced) after a document finished loading in the web view
    rendered = Signal(int, str)
    status_text = Signal(str)
    #: The user selected something in the page (Alt+select, right-click, or the shortcut):
    #: ``{bookmark_id, version_id, via, text, path, end_path}`` for the filter assistant
    selection_made = Signal(dict)
    #: The header buttons: the window owns the layout (and the shortcuts), the panel only asks.
    dock_toggle_requested = Signal()
    maximize_toggle_requested = Signal()

    def __init__(self, client: ApiClient, parent: Any = None) -> None:
        super().__init__(parent)
        self._client = client
        self._bid: int | None = None
        self._token = 0  # bumped per bookmark: guards the history list and the check log
        self._seq = 0  # bumped per render request: only the newest request may draw
        self._changes: list[ChangeOut] = []
        self._shown_version: int | None = None  # the stored version a history entry shows

        self.title = QLabel("Select a bookmark")
        self.title.setObjectName("viewerTitle")
        self.history = QComboBox()
        self.history.setObjectName("history")
        self.history.setToolTip("Unread changes, or step through each alert's own diff")
        self.deletions = QComboBox()
        for label, mode in DELETION_MODES:
            self.deletions.addItem(label, mode)
        self.images = QCheckBox("Remote images")
        self.images.setChecked(True)  # the page as it looks; unchecked = text-only placeholders
        self.images.setToolTip("Load the page's images, fonts and stylesheets from its website")
        self.changes_only = QCheckBox("Changes only")
        self.btn_dock = QToolButton()
        self.btn_dock.setObjectName("dockToggle")
        self.btn_dock.setAutoRaise(True)
        self.btn_max = QToolButton()
        self.btn_max.setObjectName("maximizeToggle")
        self.btn_max.setAutoRaise(True)
        self.set_dock("bottom")
        self.set_maximized(False)
        self.tabs = QTabBar()
        self.tabs.setObjectName("viewerTabs")
        for name in TABS:
            self.tabs.addTab(name)
        self.tabs.setFocusPolicy(self.tabs.focusPolicy().NoFocus)

        self.web = HtmlView()
        self.web.setFocusPolicy(self.web.focusPolicy().ClickFocus)
        # Screenshot diff: the overlay picture (red boxes round what changed) under a caption
        self.shot_caption = QLabel("")
        self.shot_caption.setObjectName("shotCaption")
        self.shot_caption.setWordWrap(True)
        self.shot_image = QLabel()
        self.shot_image.setObjectName("shotImage")
        self.shot_image.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.shot_scroll = QScrollArea()
        self.shot_scroll.setWidgetResizable(True)
        self.shot_scroll.setWidget(self.shot_image)
        self.shot = QWidget()
        shot_layout = QVBoxLayout(self.shot)
        shot_layout.setContentsMargins(0, 0, 0, 0)
        shot_layout.addWidget(self.shot_caption)
        shot_layout.addWidget(self.shot_scroll, 1)
        self._shot_pixmap: QPixmap | None = None
        self.log = QTableWidget(0, 5)
        self.log.setHorizontalHeaderLabels(["Started", "Trigger", "Outcome", "Reason", "ms"])
        self.log.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.web)
        self.stack.addWidget(self.shot)
        self.stack.addWidget(self.log)

        controls = QHBoxLayout()
        for w in (self.history, self.deletions, self.images, self.changes_only):
            controls.addWidget(w)
        controls.setStretch(0, 1)
        header = QHBoxLayout()
        header.addWidget(self.title, 1)
        header.addWidget(self.btn_dock)
        header.addWidget(self.btn_max)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addLayout(controls)
        layout.addWidget(self.tabs)
        layout.addWidget(self.stack, 1)

        self.tabs.currentChanged.connect(lambda _i: self.reload())
        self.history.currentIndexChanged.connect(lambda _i: self.reload())
        self.deletions.currentIndexChanged.connect(lambda _i: self.reload())
        self.images.toggled.connect(lambda _c: self.reload())
        self.changes_only.toggled.connect(lambda _c: self.reload())
        self.btn_dock.clicked.connect(lambda _c=False: self.dock_toggle_requested.emit())
        self.btn_max.clicked.connect(lambda _c=False: self.maximize_toggle_requested.emit())
        self.web.document_loaded.connect(self._on_loaded)
        self.web.bridge.selection.connect(self._on_selection)
        self._produced = ""

    # -- public -------------------------------------------------------------------------

    @property
    def bookmark_id(self) -> int | None:
        return self._bid

    def current_tab(self) -> int:
        return self.tabs.currentIndex()

    def set_dock(self, where: str, tip: str = "") -> None:
        """Label the dock button for the layout in force: it offers the other one."""
        to_right = where != "right"
        self.btn_dock.setText("Dock right" if to_right else "Dock bottom")
        self.btn_dock.setToolTip(
            tip or ("Show the viewer beside the list" if to_right else "Show the viewer below")
        )

    def set_maximized(self, on: bool, tip: str = "") -> None:
        self.btn_max.setText("Restore" if on else "Expand")
        self.btn_max.setToolTip(
            tip or ("Show the folders and the list again" if on else "Give the viewer the window")
        )

    def show_bookmark(self, bookmark_id: int | None, name: str = "") -> None:
        self._bid = bookmark_id
        self._token += 1
        self._seq += 1
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItem("Unread changes (since last read)", None)
        self.history.blockSignals(False)
        self._changes = []
        if bookmark_id is None:
            self.title.setText("Select a bookmark")
            self.web.set_document("")
            return
        self.title.setText(name)
        token = self._token
        run_async(
            lambda: self._client.changes(bookmark_id).items,
            lambda items: self._set_history(token, items),
            lambda _e: None,
        )
        self.reload()

    def reload(self) -> None:
        """Render the current tab for the current bookmark / history selection."""
        if self._bid is None:
            return
        tab = self.current_tab()
        self._seq += 1  # a response to any earlier request is stale from here on
        if tab == T_SHOT:
            self.stack.setCurrentWidget(self.shot)
            self._load_screenshot()
            return
        if tab == T_LOG:
            self.stack.setCurrentWidget(self.log)
            self._load_log()
            return
        self.stack.setCurrentWidget(self.web)
        seq, bid = self._seq, self._bid
        change_id = self.history.currentData()
        view = {T_HIGHLIGHT: "highlight", T_TEXT: "text", T_NEW: "new", T_OLD: "old"}[tab]
        images, context = self.images.isChecked(), 3 if self.changes_only.isChecked() else None
        paths = tab in ASSIST_TABS  # DOM paths let the filter assistant find what was selected
        self._shown_version = next(
            (c.new_version_id for c in self._changes if c.id == change_id), None
        )

        def fetch() -> RenderOut:
            if change_id is None:
                return self._client.unread_diff(
                    bid, view, images=images, context=context, paths=paths
                )
            return self._client.change_render(
                change_id, view, images=images, context=context, paths=paths
            )

        run_async(fetch, lambda out: self._show(seq, tab, out), lambda exc: self._error(seq, exc))

    # -- internals ----------------------------------------------------------------------

    def _set_history(self, token: int, items: list[ChangeOut]) -> None:
        if token != self._token:
            return
        self._changes = items
        self.history.blockSignals(True)
        for c in items:
            words = f"+{c.added_words or 0} −{c.removed_words or 0} words"
            span = f" over {c.checks_accumulated} checks" if c.checks_accumulated > 1 else ""
            self.history.addItem(
                f"{fmt_time(c.detected_at)}  {words}{span}  {c.summary or ''}"[:120], c.id
            )
        self.history.blockSignals(False)

    # -- filter assistant ---------------------------------------------------------------

    def request_selection(self) -> None:
        """Keyboard route to the filter assistant: use the page's current selection."""
        if self._bid is None or self.current_tab() not in ASSIST_TABS:
            self.status_text.emit("The filter assistant works in the Highlighted and New views")
            return

        def done(ok: bool) -> None:
            if not ok:
                self.status_text.emit("Select some text first (or right-click an element)")

        self.web.request_selection(done)

    def _on_selection(self, data: dict[str, Any]) -> None:
        if self._bid is None or self.current_tab() not in ASSIST_TABS:
            return
        self.selection_made.emit(
            {**data, "bookmark_id": self._bid, "version_id": self._shown_version}
        )

    def _show(self, seq: int, tab: int, out: RenderOut) -> None:
        if seq != self._seq or tab != self.current_tab():
            return
        self._produced = out.view
        self.status_text.emit(
            "Nothing unread"
            if out.identical
            else ("Large change: shown by block" if out.degraded else "")
        )
        self.web.set_document(style_document(out.html, self.deletions.currentData()))

    def _error(self, seq: int, exc: Exception) -> None:
        if seq != self._seq:
            return
        text = str(exc).split(": ", 1)[-1]
        self.web.set_document(
            "<html><head><meta name='color-scheme' content='light dark'></head>"
            f"<body><p style='font:15px sans-serif'>{htmllib.escape(text)}</p></body></html>"
        )

    # -- screenshot diff ----------------------------------------------------------------

    def _load_screenshot(self) -> None:
        seq, bid = self._seq, self._bid
        assert bid is not None
        change_id = self.history.currentData()
        run_async(
            lambda: self._client.screenshot_diff(bid, change_id),
            lambda out: self._show_screenshot(seq, out),
            lambda exc: self._screenshot_error(seq, exc),
        )

    def _show_screenshot(self, seq: int, diff: ScreenshotDiff) -> None:
        if seq != self._seq or self.current_tab() != T_SHOT:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(diff.png, "PNG"):
            self._screenshot_error(seq, ValueError("the screenshot could not be displayed"))
            return
        self._shot_pixmap = pixmap
        if diff.identical:
            caption = "Nothing unread: this is the last screenshot you read."
        elif diff.regions:
            n = diff.regions
            caption = (
                f"{n} changed region{'s' if n != 1 else ''} (boxed in red), "
                f"{diff.changed_pixels:,} pixels differ."
            )
        else:
            caption = "No visual difference from the last screenshot you read."
        self.shot_caption.setText(caption)
        self._fit_screenshot()
        self._produced = "screenshot"
        self.rendered.emit(T_SHOT, "screenshot")

    def _screenshot_error(self, seq: int, exc: Exception) -> None:
        if seq != self._seq or self.current_tab() != T_SHOT:
            return
        self._shot_pixmap = None
        self.shot_image.clear()
        if isinstance(exc, ApiError) and exc.status in (404, 409):
            text = (
                "No screenshot has been stored for this bookmark yet. Set its method to "
                "Screenshot (General tab) to compare pages visually."
            )
        else:
            text = str(exc).split(": ", 1)[-1]
        self.shot_caption.setText(text)

    def _fit_screenshot(self) -> None:
        """Scale the picture to the pane's width (never up): page screenshots are 1366 px wide."""
        pm = self._shot_pixmap
        if pm is None:
            return
        width = max(self.shot_scroll.viewport().width() - 4, 200)
        if pm.width() > width:
            pm = pm.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        self.shot_image.setPixmap(pm)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_screenshot()

    def _on_loaded(self) -> None:
        if self.web.last_html:
            self.rendered.emit(self.current_tab(), self._produced)

    def _load_log(self) -> None:
        token, bid = self._token, self._bid
        assert bid is not None

        def done(runs: list[CheckRunOut]) -> None:
            if token != self._token:
                return
            self.log.setRowCount(len(runs))
            for r, run in enumerate(runs):
                for c, text in enumerate(
                    (
                        fmt_time(run.started_at),
                        run.trigger,
                        run.outcome,
                        run.reason or "",
                        str(run.duration_ms or ""),
                    )
                ):
                    self.log.setItem(r, c, QTableWidgetItem(text))

        run_async(lambda: self._client.runs(bid).items, done, lambda _e: None)
