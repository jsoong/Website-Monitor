"""PageWatch main window: folders, bookmark list and change viewer, laid out like an email client.

The viewer sits below the folders and the list by default, so a page gets the window's full
width (docked at the right it was squeezed and horizontal layouts broke); "Dock right" gives the
classic three columns, and "Expand viewer" (F11 / Ctrl+M) hides the folders and the list so the
viewer fills the window. Both choices are remembered.

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
    QPushButton,
    QSplitter,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.models import BookmarkOut, BookmarkSummary, FolderOut, PluginInfo, Settings
from pagewatch.ui import theme
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
from pagewatch.ui.windows.filter_assistant import FilterAssistantDialog
from pagewatch.ui.windows.import_export import ImportExportDialog
from pagewatch.ui.windows.macros import MacrosDialog
from pagewatch.ui.windows.problems import ProblemsWindow
from pagewatch.ui.windows.saved_search import SavedSearchDialog
from pagewatch.ui.windows.settings import SettingsDialog
from pagewatch.ui.windows.viewer import TABS, ViewerPanel
from pagewatch.ui.workers import run_async

HEALTH_MS = 5000
REVEAL_TIMEOUT_MS = 4000  # how long "Show bookmark" waits for the list to load the row
RECONNECT_MS = 2000  # while disconnected: how often to look for a restarted engine
REFRESH_DEBOUNCE_MS = 250
SEARCH_DEBOUNCE_MS = 250
DOCK_BOTTOM, DOCK_RIGHT = "bottom", "right"
DOCKS = (DOCK_BOTTOM, DOCK_RIGHT)
# Relative sizes only (the window is not shown yet): list above viewer; folders | list | viewer.
BOTTOM_SIZES, BOTTOM_LIST_SIZES, RIGHT_SIZES = [330, 490], [240, 1100], [220, 520, 620]


class MainWindow(QMainWindow):
    def __init__(
        self,
        client: ApiClient,
        events: EventStream | None = None,
        *,
        persist: bool = False,
        data_dir: DataDir | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.events = events
        self.data_dir = data_dir  # where the engine's lockfile is; needed to follow a restart
        self._persist = persist
        self._folders: list[FolderOut] = []
        self._query: dict[str, Any] = {}
        self._pending_next: int | None = None
        self._reveal: int | None = None  # a bookmark id the list should select once it has loaded
        self.problems_window: ProblemsWindow | None = None
        self.signin_offer: list[int] = []
        self._paused = False
        self._assist_open = False  # one filter assistant at a time
        self._dock = DOCK_BOTTOM
        self._maximized = False  # the viewer has the whole window
        self._split_states: dict[str, QByteArray] = {}  # splitter sizes per layout
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

        self.middle = QWidget()
        mlayout = QVBoxLayout(self.middle)
        mlayout.setContentsMargins(0, 0, 0, 0)
        mlayout.addWidget(self.search)
        mlayout.addWidget(self.table, 1)
        # Docked below: splitter = [list_split = [tree | middle]] over [viewer]. Docked right the
        # same splitter turns horizontal and holds [tree | middle | viewer] (list_split is idle).
        self.list_split = QSplitter(Qt.Orientation.Horizontal)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.setCentralWidget(self.splitter)
        self._arrange(DOCK_BOTTOM)

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
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(RECONNECT_MS)
        self._reconnect_timer.timeout.connect(self.follow_restart)

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
        self.act_settings = self._action(
            "Settings", ["Ctrl+,"], self.open_settings, "Settings, backup and capacity"
        )
        self.act_problems = self._action(
            "Problems", ["Ctrl+Shift+P"], self.open_problems, "Failing bookmarks and failed actions"
        )
        self.act_assist = self._action(
            "Filter assistant", ["Ctrl+Shift+F"], self.viewer.request_selection,
            "Create a filter from the text selected in the page (also Alt+select or right-click)",
        )  # fmt: skip
        self.act_import = self._action(
            "Import / Export...", ["Ctrl+Shift+I"], self.import_export, "Import or export bookmarks"
        )
        self.act_saved = self._action(
            "New saved search...", [], self.new_saved_search, "Save a search as a virtual folder"
        )
        self.act_dock = self._action(
            "Dock viewer right", ["Ctrl+Shift+D"], self.toggle_viewer_dock,
            "Move the viewer between below the list and beside it",
        )  # fmt: skip
        self.act_maximize = self._action(
            "Expand viewer", ["F11", "Ctrl+M"], self.toggle_viewer_maximized,
            "Hide the folders and the list so the viewer fills the window (again to restore)",
        )  # fmt: skip
        self.act_restore = self._action(
            "Restore view", ["Esc"], self.restore_viewer, "Show the folders and the list again"
        )
        self.act_restore.setEnabled(False)  # Esc is only taken while the viewer is expanded
        menu = self.menuBar().addMenu("&File")
        menu.addAction(self.act_add)
        menu.addAction(self.act_import)
        menu.addAction(self.act_saved)
        view = self.menuBar().addMenu("&View")
        view.addAction(self.act_dock)
        view.addAction(self.act_maximize)
        view.addAction(self.act_restore)
        tools = self.menuBar().addMenu("&Tools")
        tools.addAction(self.act_assist)
        tools.addAction(self.act_fp)
        self.act_signin = self._action(
            "Sign in...", ["Ctrl+L"], self.sign_in_selected,
            "Open a window to sign in to the selected bookmark's site, then check it at once",
        )  # fmt: skip
        self.act_macros = self._action(
            "Macros", [], self.open_macros, "Record and edit the steps that log in to a site"
        )
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
        bar.addSeparator()
        bar.addAction(self.act_signin)
        bar.addAction(self.act_macros)
        bar.addAction(self.act_problems)
        bar.addAction(self.act_settings)
        bar.addSeparator()
        bar.addAction(self.act_dock)
        bar.addAction(self.act_maximize)
        self.addToolBar(bar)
        self._sync_layout_controls()

    def _build_status_bar(self) -> None:
        self.lbl_conn = QLabel("engine: connecting")
        self.lbl_queue = QLabel("queued: -")
        self.lbl_flight = QLabel("running: -")
        self.lbl_browser = QLabel("browser: -")
        self.lbl_state = QLabel("")
        self.lbl_total = QLabel("")
        self.btn_problems = QPushButton("Problems: -")
        self.btn_problems.setFlat(True)
        self.btn_problems.setToolTip("Failing bookmarks and failed actions (Ctrl+Shift+P)")
        self.btn_problems.clicked.connect(self.open_problems)
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
            self.btn_problems,
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
        self.viewer.selection_made.connect(self._open_assistant)
        self.viewer.dock_toggle_requested.connect(self.toggle_viewer_dock)
        self.viewer.maximize_toggle_requested.connect(self.toggle_viewer_maximized)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._folder_menu)
        if self.events is not None:
            self.events.event.connect(self._on_event)
            self.events.connection_changed.connect(self._on_connection)

    def apply_theme(self, name: str) -> None:
        """Dark, Light or Same as Windows - now, for every window of the application."""
        theme.apply(name)
        self.viewer.web.sync_background()

    # -- viewer layout: dock position and expand -----------------------------------------

    @property
    def viewer_dock(self) -> str:
        """``"bottom"`` (the default) or ``"right"``."""
        return self._dock

    @property
    def viewer_maximized(self) -> bool:
        return self._maximized

    def _arrange(self, where: str) -> None:
        """Put the panes in the splitters for ``where``. The viewer stays a child of the central
        splitter throughout, so its web page is never reparented (and never reloaded)."""
        s = self.splitter
        if where == DOCK_RIGHT:
            s.setOrientation(Qt.Orientation.Horizontal)
            s.insertWidget(0, self.tree)  # out of list_split, into the splitter
            s.insertWidget(1, self.middle)
            s.insertWidget(2, self.viewer)
            self.list_split.setParent(None)  # empty now; parentless widgets are hidden
            s.setSizes(RIGHT_SIZES)
        else:
            s.setOrientation(Qt.Orientation.Vertical)
            self.list_split.addWidget(self.tree)
            self.list_split.addWidget(self.middle)
            s.insertWidget(0, self.list_split)
            s.insertWidget(1, self.viewer)
            self.list_split.show()
            self.list_split.setSizes(BOTTOM_LIST_SIZES)
            s.setSizes(BOTTOM_SIZES)
        self._dock = where
        for pane in (self.tree, self.middle, self.viewer):
            pane.show()
        self._restore_split_states()
        self._apply_expanded()

    def _remember_split_states(self) -> None:
        """Keep the current layout's splitter sizes. Not while expanded: the collapsed panes would
        record a width of nothing, and the sizes from before are already kept."""
        if self._maximized:
            return
        if self._dock == DOCK_RIGHT:
            self._split_states["right"] = self.splitter.saveState()
        else:
            self._split_states["bottom"] = self.splitter.saveState()
            self._split_states["bottom_list"] = self.list_split.saveState()

    def _restore_split_states(self) -> None:
        keys = ("right",) if self._dock == DOCK_RIGHT else ("bottom_list", "bottom")
        for key in keys:
            state = self._split_states.get(key)
            if state is not None:
                (self.splitter if key in ("right", "bottom") else self.list_split).restoreState(
                    state
                )

    def _apply_expanded(self) -> None:
        """Hide (or show) the folders and the list. Below, that is the whole top pane; at the
        right, the two columns - the viewer takes the space either way."""
        hide = self._maximized
        if self._dock == DOCK_BOTTOM:
            self.list_split.setHidden(hide)
        else:
            self.tree.setHidden(hide)
            self.middle.setHidden(hide)

    def _sync_layout_controls(self) -> None:
        to_right = self._dock == DOCK_BOTTOM
        dock_keys = ", ".join(k.toString() for k in self.act_dock.shortcuts())
        max_keys = ", ".join(k.toString() for k in self.act_maximize.shortcuts())
        self.act_dock.setText("Dock viewer right" if to_right else "Dock viewer bottom")
        self.act_dock.setToolTip(
            ("Show the viewer beside the list" if to_right else "Show the viewer below the list")
            + f" ({dock_keys})"
        )
        self.act_maximize.setText("Restore viewer" if self._maximized else "Expand viewer")
        self.act_maximize.setToolTip(
            (
                "Show the folders and the list again"
                if self._maximized
                else "Give the viewer the whole window"
            )
            + f" ({max_keys}; Esc restores)"
        )
        self.act_restore.setEnabled(self._maximized)
        self.viewer.set_dock(self._dock, self.act_dock.toolTip())
        self.viewer.set_maximized(self._maximized, self.act_maximize.toolTip())

    def set_viewer_dock(self, where: str) -> None:
        """Dock the viewer ``"bottom"`` (full width under the folders and the list) or
        ``"right"`` (the classic three columns). An expanded viewer stays expanded."""
        if where not in DOCKS or where == self._dock:
            return
        self._remember_split_states()
        self._arrange(where)
        self._sync_layout_controls()
        self._save_layout_choice()

    def set_viewer_maximized(self, on: bool) -> None:
        """Give the viewer the whole window (``True``) or bring the folders and list back."""
        if on == self._maximized:
            return
        if on:
            self._remember_split_states()  # before the panes collapse
        self._maximized = on
        self._apply_expanded()
        if not on:
            self._restore_split_states()
        self._sync_layout_controls()
        self._save_layout_choice()

    def toggle_viewer_dock(self, _checked: bool = False) -> None:
        self.set_viewer_dock(DOCK_RIGHT if self._dock == DOCK_BOTTOM else DOCK_BOTTOM)

    def toggle_viewer_maximized(self, _checked: bool = False) -> None:
        self.set_viewer_maximized(not self._maximized)

    def restore_viewer(self, _checked: bool = False) -> None:
        self.set_viewer_maximized(False)

    def _save_layout_choice(self) -> None:
        if self._persist:
            s = self._settings()
            s.setValue("viewer_dock", self._dock)
            s.setValue("viewer_maximized", self._maximized)

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
        if self._reveal is not None:
            row = next(
                (
                    r
                    for r in range(self.model.rowCount())
                    if self.model.summary(r).id == self._reveal
                ),
                None,
            )
            if row is not None:
                self._reveal = None
                self._select_row(row)
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
        def load() -> tuple[list[FolderOut], Any, int]:
            return self.client.folders(), self.client.counts(), self.client.problems(1).total

        def done(res: tuple[list[FolderOut], Any, int]) -> None:
            self._folders = res[0]
            self.tree.load(res[0], res[1])
            self.set_problem_count(res[2])

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
            self._reconnect_timer.stop()
            self._refresh_side()
            self.model.reload()
            self.refresh_health()
        elif self.data_dir is not None:
            self._reconnect_timer.start()

    def follow_restart(self) -> bool:
        """Look for an engine that restarted (after a restore, a memory-guard exit or by hand):
        it comes back on a new port with a new token, so the old address never answers again.
        Returns True when the window was pointed at a new engine."""
        if self.data_dir is None or self.events is None:
            return False
        info = read_lockfile(self.data_dir)  # None while the engine is down or still starting
        base_url = f"http://127.0.0.1:{info.port}" if info is not None else ""
        if info is None or (self.client.token == info.token and self.client.base_url == base_url):
            return False
        self.client.retarget(base_url, info.token)
        self.events.retarget(self.client.ws_url, info.token)
        self.say("The engine restarted: reconnecting")
        return True

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
        elif kind == "signin":
            self._on_signin_event(data)
        elif kind in ("change_detected", "check_finished", "engine_state", "problem"):
            if kind == "problem" and data.get("kind") == "action_failed":
                self.say(f"An action failed: {data.get('error', '')}"[:200])
            if kind == "engine_state" and data.get("reason") == "signin_prompt":
                self.offer_signin(list(data.get("bookmark_ids") or []))
            self._debounce.start()
            if self.problems_window is not None and self.problems_window.isVisible():
                self.problems_window.schedule_refresh()
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

    # -- logins -------------------------------------------------------------------------

    def sign_in_selected(self) -> None:
        """Open the sign-in window for the selected bookmark (spec: semi-attended sign-in)."""
        targets = self.selected_summaries()
        if len(targets) != 1:
            self.say("Select one bookmark to sign in to.")
            return
        target = targets[0]
        self.say(f"Opening a sign-in window for {target.name}...")
        run_async(
            lambda: self.client.start_signin(target.id),
            lambda _o: self.say(
                f"Sign in to {target.name} in the window that opened, then close it: "
                "PageWatch checks as soon as you do."
            ),
            lambda e: self.say(str(e).split(": ", 1)[-1]),
        )

    def _on_signin_event(self, data: dict[str, Any]) -> None:
        bid = data.get("bookmark_id")
        if data.get("state") == "finished":
            self.say("Signed in. Checking now.")
        if isinstance(bid, int):
            self.model.refresh_bookmark(bid)
        self._debounce.start()

    def offer_signin(self, ids: list[int]) -> None:
        """The daily prompt (spec: "A daily prompt offers to sign in again"). A line in the
        status bar and a count on the toolbar button: never a dialog that steals focus."""
        self.signin_offer = ids
        self.act_signin.setText(f"Sign in... ({len(ids)})" if ids else "Sign in...")
        if ids:
            self.say(
                f"{len(ids)} bookmark(s) could use a fresh sign-in. Select one and choose "
                "Sign in..., or ignore this."
            )

    def open_macros(self) -> None:
        self._exec(MacrosDialog(self.client, self))

    # -- problems -----------------------------------------------------------------------

    def set_problem_count(self, n: int) -> None:
        self.btn_problems.setText(f"Problems: {n}")
        self.btn_problems.setStyleSheet("color: #c62828; font-weight: bold" if n else "")

    def open_problems(self) -> None:
        if self.problems_window is None:
            win = ProblemsWindow(self.client, self)
            win.show_bookmark.connect(self.reveal_bookmark)
            win.count_changed.connect(self.set_problem_count)
            self.problems_window = win
        else:
            self.problems_window.refresh()
        self.problems_window.show()
        self.problems_window.raise_()
        self.problems_window.activateWindow()

    def reveal_bookmark(self, bookmark_id: int, url: str) -> None:
        """Select a bookmark in the list wherever it is: back to "All bookmarks", search for its
        address, and select the row when the engine's page with it has loaded."""
        self._reveal = bookmark_id
        QTimer.singleShot(REVEAL_TIMEOUT_MS, lambda: self._give_up_reveal(bookmark_id))
        self.tree.select_all()
        self.search.blockSignals(True)
        self.search.setText(url)
        self.search.blockSignals(False)
        self._search_timer.stop()
        self._apply_query()
        self.raise_()
        self.activateWindow()

    def _give_up_reveal(self, bookmark_id: int) -> None:
        if self._reveal == bookmark_id:
            self._reveal = None
            self.say("That bookmark is no longer in the list")

    def open_settings(self) -> None:
        def done(settings: Settings) -> None:
            dialog = SettingsDialog(self.client, settings, self.data_dir, self)
            dialog.saved.connect(self.refresh_health)
            dialog.saved.connect(lambda: self.apply_theme(dialog.settings.theme))
            dialog.restore_staged.connect(
                lambda _p: self.say("Restoring a backup: the engine is restarting")
            )
            self._exec(dialog)

        run_async(self.client.settings, done, lambda e: self.say(str(e)))

    # -- dialogs ------------------------------------------------------------------------

    def add_bookmark(self) -> None:
        dialog = AddBookmarkDialog(self.client, self._folders, self.tree.selected_folder_id(), self)
        dialog.created.connect(lambda _o: (self.model.reload(), self._refresh_side()))
        self._exec(dialog)

    def edit_selected(self) -> None:
        targets = self.selected_summaries()
        if not targets:
            return

        def load() -> tuple[list[BookmarkOut], list[PluginInfo]]:
            try:
                plugins = [p for p in self.client.plugins() if p.installed]
            except Exception:  # the editor works without the list
                plugins = []
            return [self.client.bookmark(b.id) for b in targets], plugins

        def done(res: tuple[list[BookmarkOut], list[PluginInfo]]) -> None:
            outs, plugins = res
            dialog = BookmarkEditor(self.client, outs, self._folders, self, plugins=plugins)
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
        saved_new = menu.addAction("New saved search...")
        item = self.tree.itemAt(pos)
        fid = self.tree.selected_folder_id() if item is not None else None
        sid = self.tree.selected_saved_id() if item is not None else None
        rm = menu.addAction("Delete folder") if fid is not None else None
        saved_edit = menu.addAction("Edit saved search...") if sid is not None else None
        saved_rm = menu.addAction("Delete saved search") if sid is not None else None
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is new:
            name, ok = QInputDialog.getText(self, "New folder", "Name")
            if ok and name.strip():
                body = {"name": name.strip(), "parent_id": fid}
                run_async(
                    lambda: self.client.create_folder(body),
                    lambda _o: self._refresh_side(),
                    lambda e: self.say(str(e)),
                )
        elif chosen is saved_new:
            self.new_saved_search()
        elif saved_edit is not None and chosen is saved_edit and sid is not None:
            self.edit_saved_search(sid)
        elif (rm is not None and chosen is rm) or (saved_rm is not None and chosen is saved_rm):
            target = fid if (rm is not None and chosen is rm) else sid
            assert target is not None
            run_async(
                lambda: self.client.delete_folder(target),
                lambda _o: (self._refresh_side(), self.model.reload()),
                lambda e: self.say(str(e)),
            )

    # -- saved searches (virtual folders) -----------------------------------------------

    def _current_search(self) -> dict[str, Any]:
        """The list as it is now, as the criteria of a saved search."""
        q: dict[str, Any] = {}
        for key in ("status", "unread", "keyword_hits"):
            if key in self._query:
                q[key] = self._query[key]
        folder = self._query.get("folder")
        if isinstance(folder, int) and self.tree.selected_folder_id() == folder:
            q["folder"] = folder
        text = self.search.text().strip()
        if text:
            q["q"] = text
        return q

    def new_saved_search(self) -> None:
        dialog = SavedSearchDialog(self._folders, query=self._current_search(), parent=self)
        if not self._exec(dialog):
            return
        body = {"name": dialog.name(), "is_virtual": True, "query": dialog.query()}
        run_async(
            lambda: self.client.create_folder(body),
            lambda f: (
                self._refresh_side(),
                self.say(f"Saved search \u201c{f.name}\u201d created"),
            ),
            lambda e: self.say(str(e).split(": ", 1)[-1]),
        )

    def edit_saved_search(self, folder_id: int) -> None:
        current = next((f for f in self._folders if f.id == folder_id), None)
        if current is None:
            return
        dialog = SavedSearchDialog(
            self._folders, name=current.name, query=current.query or {}, parent=self
        )
        if not self._exec(dialog):
            return
        body = {"name": dialog.name(), "query": dialog.query()}
        run_async(
            lambda: self.client.patch_folder(folder_id, body),
            lambda _f: (self._refresh_side(), self.model.reload()),
            lambda e: self.say(str(e).split(": ", 1)[-1]),
        )

    # -- import / export ----------------------------------------------------------------

    def import_export(self) -> None:
        dialog = ImportExportDialog(
            self.client,
            self._folders,
            selected_ids=[b.id for b in self.selected_summaries()],
            folder_id=self.tree.selected_folder_id(),
            parent=self,
        )
        dialog.changed.connect(lambda: (self.model.reload(), self._refresh_side()))
        self._exec(dialog)

    # -- filter assistant ---------------------------------------------------------------

    def _open_assistant(self, sel: dict[str, Any]) -> None:
        """Alt+select, right-click or Ctrl+Shift+F in the page: offer filters for the selection,
        show each one's test result, and save the one the user picks."""
        bid = sel.get("bookmark_id")
        if not isinstance(bid, int) or self._assist_open:
            return
        body: dict[str, Any] = {"path": sel["path"], "text": sel.get("text", "")}
        for key in ("end_path", "version_id"):
            if sel.get(key) is not None:
                body[key] = sel[key]
        self._assist_open = True

        def done(out: Any) -> None:
            try:
                dialog = FilterAssistantDialog(self.client, bid, body, out, self)
                if self._exec(dialog):
                    patch = dialog.patch()
                    if patch:
                        run_async(
                            lambda: self.client.patch_bookmark(bid, patch),
                            lambda _o: self.say("Filter saved: the next check will use it"),
                            lambda e: self.say(str(e).split(": ", 1)[-1]),
                        )
            finally:
                self._assist_open = False

        def failed(exc: Exception) -> None:
            self._assist_open = False
            self.say(str(exc).split(": ", 1)[-1])

        run_async(lambda: self.client.filter_assist(bid, body), done, failed)

    # -- persistence / lifecycle --------------------------------------------------------

    def _settings(self) -> QSettings:
        return QSettings("PageWatch", "PageWatch")

    def _restore(self) -> None:
        s = self._settings()
        geo = s.value("geometry")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        for key, name in (
            ("splitter_bottom", "bottom"),
            ("splitter_bottom_list", "bottom_list"),
            ("splitter_right", "right"),
        ):
            state = s.value(key)
            if isinstance(state, QByteArray) and name not in self._split_states:
                self._split_states[name] = state
        dock = s.value("viewer_dock", DOCK_BOTTOM)
        self._maximized = bool(s.value("viewer_maximized", False, type=bool))
        self._arrange(dock if dock in DOCKS else DOCK_BOTTOM)  # also hides the panes if expanded
        self._sync_layout_controls()

    def closeEvent(self, event: Any) -> None:
        """Closing the window never touches the engine: it keeps checking."""
        if self._persist:
            s = self._settings()
            s.setValue("geometry", self.saveGeometry())
            self._remember_split_states()
            for name, key in (
                ("bottom", "splitter_bottom"),
                ("bottom_list", "splitter_bottom_list"),
                ("right", "splitter_right"),
            ):
                if name in self._split_states:
                    s.setValue(key, self._split_states[name])
            self._save_layout_choice()
        if self.problems_window is not None:
            self.problems_window.close()
        self._health_timer.stop()
        self._reconnect_timer.stop()
        self._search_timer.stop()
        self._debounce.stop()
        if self.events is not None:
            self.events.stop()
        super().closeEvent(event)
