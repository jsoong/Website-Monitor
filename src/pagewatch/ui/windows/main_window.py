"""PageWatch main window: folders | bookmark list | change viewer, laid out like an email client.

The UI never fetches pages and never owns monitoring: closing this window leaves the engine
checking. Every action is a call to the local API on a worker thread; engine events keep the
list, counts and viewer fresh.

Review shortcuts (work from any pane): N or Space next unread, R mark read, O open URL,
F flag false positive, C check selected now, Ctrl+E edit.
"""

from __future__ import annotations

import webbrowser
from typing import Any

from PySide6.QtCore import QByteArray, QModelIndex, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import BookmarkOut, BookmarkSummary, FolderOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.models import (
    COL_NAME,
    BookmarkListModel,
    FolderTree,
    SummaryRole,
)
from pagewatch.ui.windows.add_assistant import AddBookmarkDialog
from pagewatch.ui.windows.bookmark_editor import BookmarkEditor
from pagewatch.ui.windows.false_positive import FalsePositiveDialog
from pagewatch.ui.windows.viewer import TABS, ViewerPanel
from pagewatch.ui.workers import run_async

HEALTH_MS = 5000
REFRESH_DEBOUNCE_MS = 250
SEARCH_DEBOUNCE_MS = 250


class MainWindow(QMainWindow):
    def __init__(
        self,
        client: ApiClient,
        events: EventStream | None = None,
        *,
        persist: bool = False,
    ) -> None:
        super().__init__()
        self.client = client
        self.events = events
        self._persist = persist
        self._folders: list[FolderOut] = []
        self._query: dict[str, Any] = {}
        self._pending_next: int | None = None
        self._paused = False
        self.setWindowTitle("PageWatch")
        self.resize(1360, 820)

        self.model = BookmarkListModel(client, self)
        self.tree = FolderTree()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or URL (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.table = QTableView()
        self.table.setObjectName("bookmarkList")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        self.table.setColumnWidth(0, 28)
        self.table.setColumnWidth(COL_NAME, 260)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sortIndicatorChanged.connect(self.model.sort)  # sorting is the engine's job
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.viewer = ViewerPanel(client)

        middle = QWidget()
        mlayout = QVBoxLayout(middle)
        mlayout.setContentsMargins(0, 0, 0, 0)
        mlayout.addWidget(self.search)
        mlayout.addWidget(self.table, 1)
        self.splitter = QSplitter()
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(middle)
        self.splitter.addWidget(self.viewer)
        self.splitter.setSizes([220, 520, 620])
        self.setCentralWidget(self.splitter)

        self._build_actions()
        self._build_status_bar()
        self._wire()
        if persist:
            self._restore()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(REFRESH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._refresh_side)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_query)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(HEALTH_MS)
        self._health_timer.timeout.connect(self.refresh_health)
        self._health_timer.start()

        self._refresh_side()
        self.refresh_health()
        self.model.set_query()

    # -- construction -------------------------------------------------------------------

    def _action(self, text: str, keys: list[str], slot: Any, tip: str = "") -> QAction:
        act = QAction(text, self)
        if keys:
            act.setShortcuts([QKeySequence(k) for k in keys])
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        act.setToolTip(f"{tip or text}" + (f" ({', '.join(keys)})" if keys else ""))
        act.triggered.connect(slot)
        self.addAction(act)  # active even when the toolbar is hidden
        return act

    def _build_actions(self) -> None:
        self.act_add = self._action("Add", ["Ctrl+N"], self.add_bookmark, "Add a bookmark")
        self.act_check = self._action(
            "Check selected", ["C"], self.check_selected, "Check the selected bookmarks now"
        )
        self.act_check_all = self._action("Check all", ["Ctrl+Shift+C"], self.check_all)
        self.act_autowatch = self._action(
            "Pause AutoWatch", ["Ctrl+P"], self.toggle_autowatch, "Start or pause AutoWatch"
        )
        self.act_read = self._action("Mark read", ["R"], self.mark_read, "Mark the selection read")
        self.act_next = self._action("Next unread", ["N", "Space"], self.next_unread)
        self.act_open = self._action(
            "Open URL", ["O"], self.open_url, "Open the page in your browser"
        )
        self.act_fp = self._action(
            "False positive", ["F"], self.flag_false_positive, "Flag the change as a false positive"
        )
        self.act_edit = self._action(
            "Edit", ["Ctrl+E"], self.edit_selected, "Edit the selected bookmark(s)"
        )
        self.act_search = self._action(
            "Search", ["Ctrl+F"], lambda: self.search.setFocus(), "Search"
        )
        self.act_delete = self._action("Delete", ["Delete"], self.delete_selected)
        for i, name in enumerate(TABS):  # keyboard access to every viewer tab
            self._action(
                f"Show {name}",
                [f"Ctrl+{i + 1}"],
                lambda _c=False, i=i: self.viewer.tabs.setCurrentIndex(i),
            )
        bar = QToolBar("Main")
        bar.setMovable(False)
        for act in (
            self.act_add,
            self.act_check,
            self.act_check_all,
            self.act_autowatch,
            self.act_read,
            self.act_next,
        ):
            bar.addAction(act)
        self.addToolBar(bar)

    def _build_status_bar(self) -> None:
        self.lbl_conn = QLabel("engine: connecting")
        self.lbl_queue = QLabel("queued: -")
        self.lbl_flight = QLabel("running: -")
        self.lbl_browser = QLabel("browser: -")
        self.lbl_state = QLabel("")
        self.lbl_total = QLabel("")
        self.lbl_message = QLabel("")  # feedback for actions ("Queued 3 checks", ...)
        self.lbl_view = QLabel("")  # what the viewer says about what it shows
        bar = self.statusBar()
        bar.addPermanentWidget(self.lbl_view)
        for w in (
            self.lbl_conn,
            self.lbl_queue,
            self.lbl_flight,
            self.lbl_browser,
            self.lbl_state,
            self.lbl_total,
        ):
            bar.addPermanentWidget(w)
        bar.addWidget(self.lbl_message, 1)

    def _wire(self) -> None:
        self.tree.query_selected.connect(self._folder_query)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.table.selectionModel().currentRowChanged.connect(self._current_changed)
        self.model.page_loaded.connect(self._page_loaded)
        self.model.load_failed.connect(lambda m: self.say(f"Could not load bookmarks: {m}"))
        self.viewer.status_text.connect(self.lbl_view.setText)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._folder_menu)
        if self.events is not None:
            self.events.event.connect(self._on_event)
            self.events.connection_changed.connect(self._on_connection)

    # -- small helpers ------------------------------------------------------------------

    def say(self, text: str) -> None:
        self.lbl_message.setText(text)

    def _exec(self, dialog: QDialog) -> bool:
        """Run a modal dialog; a seam so tests can intercept dialogs."""
        return bool(dialog.exec() == QDialog.DialogCode.Accepted)

    def current_summary(self) -> BookmarkSummary | None:
        idx = self.table.currentIndex()
        return idx.data(SummaryRole) if idx.isValid() else None

    def selected_summaries(self) -> list[BookmarkSummary]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        out = [self.model.summary(r) for r in rows]
        if not out and (cur := self.current_summary()):
            out = [cur]
        return out

    def _select_row(self, row: int) -> None:
        idx = self.model.index(row, COL_NAME)
        self.table.selectionModel().setCurrentIndex(
            idx,
            self.table.selectionModel().SelectionFlag.ClearAndSelect
            | self.table.selectionModel().SelectionFlag.Rows,
        )
        self.table.scrollTo(idx)

    # -- list / folder / search ---------------------------------------------------------

    def _folder_query(self, query: dict[str, Any]) -> None:
        self._query = query
        self._apply_query()

    def _apply_query(self) -> None:
        self.model.set_query(**self._query, q=self.search.text().strip())

    def _page_loaded(self) -> None:
        self.lbl_total.setText(f"{self.model.total:,} bookmarks")
        if self._pending_next is not None:
            start, self._pending_next = self._pending_next, None
            self._advance(start)
        elif (
            self.model.rowCount()
            and not self.table.currentIndex().isValid()
            and self.table.selectionModel().selectedRows() == []
        ):
            pass  # nothing selected yet: leave it to the user

    def _current_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if not current.isValid():
            self.viewer.show_bookmark(None)
            return
        b: BookmarkSummary = self.model.summary(current.row())
        self.viewer.show_bookmark(b.id, b.name)

    def _refresh_side(self) -> None:
        def load() -> tuple[list[FolderOut], Any]:
            return self.client.folders(), self.client.counts()

        def done(res: tuple[list[FolderOut], Any]) -> None:
            self._folders = res[0]
            self.tree.load(res[0], res[1])

        run_async(load, done, lambda _e: None)

    def refresh_health(self) -> None:
        def done(h: Any) -> None:
            self.lbl_conn.setText("engine: connected")
            self.lbl_queue.setText(
                f"queued: {h.queue_length}" + (" (backlog!)" if h.backlog_warning else "")
            )
            self.lbl_flight.setText(f"running: {h.in_flight}")
            self.lbl_browser.setText(f"browser: {h.browser_state}")
            flags = [("offline" if not h.online else ""), ("on battery" if h.on_battery else ""),
                     ("AutoWatch paused" if h.autowatch.state == "paused" else "")]  # fmt: skip
            self.lbl_state.setText(", ".join(f for f in flags if f))
            self._paused = h.autowatch.state == "paused"
            self.act_autowatch.setText("Start AutoWatch" if self._paused else "Pause AutoWatch")

        run_async(
            self.client.health, done, lambda _e: self.lbl_conn.setText("engine: not reachable")
        )

    # -- events -------------------------------------------------------------------------

    def _on_connection(self, up: bool) -> None:
        self.lbl_conn.setText("engine: connected" if up else "engine: disconnected")
        if up:
            self._refresh_side()
            self.model.reload()

    def _on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "bookmark_updated":
            bid = data.get("bookmark_id")
            if data.get("created") or data.get("deleted"):
                self.model.reload()
            elif isinstance(bid, int):
                self.model.refresh_bookmark(bid)
                cur = self.current_summary()
                if cur is not None and cur.id == bid:
                    self.viewer.show_bookmark(bid, cur.name)
            self._debounce.start()
        elif kind in ("change_detected", "check_finished", "engine_state", "problem"):
            self._debounce.start()
            if kind != "check_finished":
                self.refresh_health()

    # -- review actions -----------------------------------------------------------------

    def _find_unread(self, start: int, stop: int | None = None) -> int | None:
        for r in range(start, self.model.rowCount() if stop is None else stop):
            if self.model.summary(r).unread:
                return r
        return None

    def next_unread(self) -> None:
        cur = self.table.currentIndex()
        self._advance(cur.row() + 1 if cur.isValid() else 0)

    def _advance(self, start: int) -> None:
        row = self._find_unread(start)
        if row is not None:
            self._select_row(row)
            self.say("")
            return
        if self.model.canFetchMore():
            self._pending_next = self.model.rowCount()
            self.model.fetchMore()
            return
        if self.model.loading:
            self._pending_next = start
            return
        wrapped = self._find_unread(0, max(0, start))  # nothing after: look before the selection
        if wrapped is not None:
            self._select_row(wrapped)
            return
        self.say("No more unread changes")

    def mark_read(self) -> None:
        targets = list(self.selected_summaries())
        if not targets:
            return

        def work() -> list[BookmarkOut]:
            return [self.client.mark_read(b.id) for b in targets]

        def done(outs: list[BookmarkOut]) -> None:
            for out in outs:
                self.model.refresh_bookmark(out.id)
            cur = self.current_summary()
            if cur is not None and cur.id in {o.id for o in outs}:
                self.viewer.show_bookmark(cur.id, cur.name)
            self._debounce.start()
            self.say(f"Marked {len(outs)} bookmark(s) read")

        run_async(work, done, lambda e: self.say(str(e)))

    def open_url(self) -> None:
        cur = self.current_summary()
        if cur is not None:
            self._open_external(cur.url)

    def _open_external(self, url: str) -> None:
        webbrowser.open(url)

    def flag_false_positive(self) -> None:
        cur = self.current_summary()
        if cur is None:
            return

        def work() -> Any:
            changes = self.client.changes(cur.id, limit=1).items
            return self.client.false_positive(changes[0].id) if changes else None

        def done(result: Any) -> None:
            if result is None:
                self.say("This bookmark has no change to flag")
                return
            dialog = FalsePositiveDialog(result, self)
            if self._exec(dialog):
                patch = dialog.patch()
                run_async(
                    lambda: self.client.patch_bookmark(cur.id, patch),
                    lambda _o: self.say("Filter added: the next check will use it"),
                    lambda e: self.say(str(e)),
                )

        run_async(work, done, lambda e: self.say(str(e).split(": ", 1)[-1]))

    def check_selected(self) -> None:
        ids = [b.id for b in self.selected_summaries()]
        if ids:
            run_async(
                lambda: self.client.check(ids),
                lambda n: self.say(f"Queued {n} check(s)"),
                lambda e: self.say(str(e)),
            )

    def check_all(self) -> None:
        run_async(
            lambda: self.client.check(all=True),
            lambda n: self.say(f"Queued {n} check(s)"),
            lambda e: self.say(str(e)),
        )

    def toggle_autowatch(self) -> None:
        state = "running" if self._paused else "paused"

        def done(_s: Any) -> None:
            self.refresh_health()

        run_async(lambda: self.client.autowatch(state), done, lambda e: self.say(str(e)))

    # -- dialogs ------------------------------------------------------------------------

    def add_bookmark(self) -> None:
        dialog = AddBookmarkDialog(self.client, self._folders, self.tree.selected_folder_id(), self)
        dialog.created.connect(lambda _o: (self.model.reload(), self._refresh_side()))
        self._exec(dialog)

    def edit_selected(self) -> None:
        targets = self.selected_summaries()
        if not targets:
            return

        def load() -> list[BookmarkOut]:
            return [self.client.bookmark(b.id) for b in targets]

        def done(outs: list[BookmarkOut]) -> None:
            dialog = BookmarkEditor(self.client, outs, self._folders, self)
            dialog.saved.connect(lambda: [self.model.refresh_bookmark(o.id) for o in outs])
            self._exec(dialog)

        run_async(load, done, lambda e: self.say(str(e)))

    def delete_selected(self) -> None:
        targets = self.selected_summaries()
        if not targets:
            return
        names = ", ".join(b.name for b in targets[:3]) + ("..." if len(targets) > 3 else "")
        answer = QMessageBox.question(
            self, "Delete bookmarks", f"Delete {len(targets)} bookmark(s)?\n{names}"
        )
        if answer == QMessageBox.StandardButton.Yes:
            ids = [b.id for b in targets]
            run_async(
                lambda: self.client.bulk(ids, "delete"),
                lambda _n: self.model.reload(),
                lambda e: self.say(str(e)),
            )

    def _row_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        for act in (
            self.act_check,
            self.act_read,
            self.act_open,
            self.act_edit,
            self.act_fp,
            self.act_delete,
        ):
            menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _folder_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        new = menu.addAction("New folder...")
        item = self.tree.itemAt(pos)
        fid = self.tree.selected_folder_id() if item is not None else None
        rm = menu.addAction("Delete folder") if fid is not None else None
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is new:
            name, ok = QInputDialog.getText(self, "New folder", "Name")
            if ok and name.strip():
                body = {"name": name.strip(), "parent_id": fid}
                run_async(
                    lambda: self.client._call("POST", "/folders", json=body),
                    lambda _o: self._refresh_side(),
                    lambda e: self.say(str(e)),
                )
        elif rm is not None and chosen is rm:
            run_async(
                lambda: self.client._call("DELETE", f"/folders/{fid}"),
                lambda _o: (self._refresh_side(), self.model.reload()),
                lambda e: self.say(str(e)),
            )

    # -- persistence / lifecycle --------------------------------------------------------

    def _settings(self) -> QSettings:
        return QSettings("PageWatch", "PageWatch")

    def _restore(self) -> None:
        s = self._settings()
        geo, split = s.value("geometry"), s.value("splitter")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        if isinstance(split, QByteArray):
            self.splitter.restoreState(split)

    def closeEvent(self, event: Any) -> None:
        """Closing the window never touches the engine: it keeps checking."""
        if self._persist:
            s = self._settings()
            s.setValue("geometry", self.saveGeometry())
            s.setValue("splitter", self.splitter.saveState())
        self._health_timer.stop()
        self._search_timer.stop()
        self._debounce.stop()
        if self.events is not None:
            self.events.stop()
        super().closeEvent(event)
