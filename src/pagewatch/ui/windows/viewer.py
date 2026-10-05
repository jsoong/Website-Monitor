"""The change viewer: one shared web view behind a row of tabs.

Highlighted and Text diff show *last read -> latest* (everything unread) by default; the
history list steps through each alert's own diff. All HTML comes from the engine, already
sanitised; the view additionally refuses to navigate and sends link clicks to the OS browser.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap, QResizeEvent
from PySide6.QtWebEngineCore import QWebEnginePage
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
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import ChangeOut, CheckRunOut, RenderOut
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


def _ui_css() -> str:
    return resources.files("pagewatch.ui").joinpath("web/diff.css").read_text(encoding="utf-8")


class _Page(QWebEnginePage):
    """Never navigates away from the rendered change: links open in the default browser."""

    def acceptNavigationRequest(
        self, url: QUrl, type_: QWebEnginePage.NavigationType, is_main_frame: bool
    ) -> bool:
        if type_ == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return True


class HtmlView(QWebEngineView):
    document_loaded = Signal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setPage(_Page(self))
        self.loadFinished.connect(lambda _ok: self.document_loaded.emit())
        self.last_html = ""

    def set_document(self, html: str) -> None:
        self.last_html = html
        self.setHtml(html, QUrl("about:blank"))


def style_document(html: str, deletions: str) -> str:
    """Apply the deletions toggle (a class on <body>) and the UI's extra stylesheet."""
    html = html.replace("pw-del-inline", f"pw-del-{deletions}", 1)
    return html.replace("</style>", _ui_css() + "</style>", 1)


class ViewerPanel(QWidget):
    #: (tab index, view produced) after a document finished loading in the web view
    rendered = Signal(int, str)
    status_text = Signal(str)

    def __init__(self, client: ApiClient, parent: Any = None) -> None:
        super().__init__(parent)
        self._client = client
        self._bid: int | None = None
        self._token = 0  # bumped per bookmark: guards the history list and the check log
        self._seq = 0  # bumped per render request: only the newest request may draw
        self._changes: list[ChangeOut] = []

        self.title = QLabel("Select a bookmark")
        self.title.setObjectName("viewerTitle")
        self.history = QComboBox()
        self.history.setObjectName("history")
        self.history.setToolTip("Unread changes, or step through each alert's own diff")
        self.deletions = QComboBox()
        for label, mode in DELETION_MODES:
            self.deletions.addItem(label, mode)
        self.images = QCheckBox("Remote images")
        self.changes_only = QCheckBox("Changes only")
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addLayout(controls)
        layout.addWidget(self.tabs)
        layout.addWidget(self.stack, 1)

        self.tabs.currentChanged.connect(lambda _i: self.reload())
        self.history.currentIndexChanged.connect(lambda _i: self.reload())
        self.deletions.currentIndexChanged.connect(lambda _i: self.reload())
        self.images.toggled.connect(lambda _c: self.reload())
        self.changes_only.toggled.connect(lambda _c: self.reload())
        self.web.document_loaded.connect(self._on_loaded)
        self._produced = ""

    # -- public -------------------------------------------------------------------------

    @property
    def bookmark_id(self) -> int | None:
        return self._bid

    def current_tab(self) -> int:
        return self.tabs.currentIndex()

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

        def fetch() -> RenderOut:
            if change_id is None:
                return self._client.unread_diff(bid, view, images=images, context=context)
            return self._client.change_render(change_id, view, images=images, context=context)

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
            f"<html><body><p style='font:15px sans-serif'>{text}</p></body></html>"
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
