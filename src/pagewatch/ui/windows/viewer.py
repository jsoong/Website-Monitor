"""The change viewer: one shared web view behind a row of tabs.

Highlighted and Text diff show *last read -> latest* (everything unread) by default; the
history list steps through each alert's own diff. All HTML comes from the engine, already
sanitised; the view additionally refuses to navigate and sends link clicks to the OS browser.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import ChangeOut, CheckRunOut, RenderOut
from pagewatch.ui.client import ApiClient
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
        self._token = 0
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
        self.shot = QLabel("No screenshot for this bookmark.")
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
        if tab == T_SHOT:
            self.stack.setCurrentWidget(self.shot)
            return
        if tab == T_LOG:
            self.stack.setCurrentWidget(self.log)
            self._load_log()
            return
        self.stack.setCurrentWidget(self.web)
        token, bid = self._token, self._bid
        change_id = self.history.currentData()
        view = {T_HIGHLIGHT: "highlight", T_TEXT: "text", T_NEW: "new", T_OLD: "old"}[tab]
        images, context = self.images.isChecked(), 3 if self.changes_only.isChecked() else None

        def fetch() -> RenderOut:
            if change_id is None:
                return self._client.unread_diff(bid, view, images=images, context=context)
            return self._client.change_render(change_id, view, images=images, context=context)

        run_async(
            fetch, lambda out: self._show(token, tab, out), lambda exc: self._error(token, exc)
        )

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

    def _show(self, token: int, tab: int, out: RenderOut) -> None:
        if token != self._token or tab != self.current_tab():
            return
        self._produced = out.view
        self.status_text.emit(
            "Nothing unread"
            if out.identical
            else ("Large change: shown by block" if out.degraded else "")
        )
        self.web.set_document(style_document(out.html, self.deletions.currentData()))

    def _error(self, token: int, exc: Exception) -> None:
        if token != self._token:
            return
        text = str(exc).split(": ", 1)[-1]
        self.web.set_document(
            f"<html><body><p style='font:15px sans-serif'>{text}</p></body></html>"
        )

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
