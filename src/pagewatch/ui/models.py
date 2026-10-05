"""Qt models: the lazily-paged bookmark list and the folder tree.

The list never holds more than the pages the user has scrolled to. Sorting and filtering are
done by the engine (keyset-paged ``/bookmarks``), so with 10,000 bookmarks a sort or a filter
is one small request, not a client-side pass over every row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from pagewatch.engine.clock import ISO_FMT
from pagewatch.models import BookmarkCounts, BookmarkOut, BookmarkSummary, FolderOut, ScheduleMode
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

PAGE_SIZE = 200
SummaryRole = Qt.ItemDataRole.UserRole + 1

# (header, server sort key or None when the column cannot be sorted by the engine)
COLUMNS: list[tuple[str, str | None]] = [
    ("", "status"),
    ("Name", "name"),
    ("Last changed", "last_changed"),
    ("Last checked", "last_checked"),
    ("Next check", "next_due"),
    ("Every", None),
    ("Errors", "errors"),
    ("Keywords", None),
]
COL_STATUS, COL_NAME = 0, 1

STATUS_COLORS = {
    "new": "#9e9e9e",
    "ok": "#43a047",
    "changed": "#fb8c00",
    "error": "#e53935",
    "needs_login": "#8e24aa",
    "disabled": "#cfd8dc",
}
_icon_cache: dict[str, QIcon] = {}


def status_icon(status: str) -> QIcon:
    icon = _icon_cache.get(status)
    if icon is None:
        pm = QPixmap(14, 14)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(STATUS_COLORS.get(status, "#9e9e9e")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 10, 10)
        p.end()
        icon = _icon_cache[status] = QIcon(pm)
    return icon


def fmt_time(value: str | None) -> str:
    if not value:
        return ""
    dt = datetime.strptime(value, ISO_FMT).replace(tzinfo=UTC).astimezone()
    return dt.strftime("%b %d %H:%M")


def fmt_interval(secs: int | None) -> str:
    if not secs:
        return ""
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def summary_from_out(out: BookmarkOut, previous: BookmarkSummary | None = None) -> BookmarkSummary:
    adaptive = out.schedule.mode is ScheduleMode.ADAPTIVE
    return BookmarkSummary(
        id=out.id,
        folder_id=out.folder_id,
        name=out.name,
        url=out.url,
        source_type=out.source_type,
        check_method=out.check_method,
        enabled=out.enabled,
        priority=out.priority,
        status=out.status,
        unread=out.unread,
        consecutive_errors=out.consecutive_errors,
        interval_s=out.current_interval_s if adaptive else out.schedule.interval_s,
        schedule_mode=out.schedule.mode,
        next_due_at=out.next_due_at,
        last_checked_at=out.last_checked_at,
        last_changed_at=out.last_changed_at,
        keyword_hits=(previous.keyword_hits if previous and out.unread else []),
    )


class BookmarkListModel(QAbstractTableModel):
    """Rows arrive page by page through ``fetchMore``; every request is guarded by a generation
    counter so a stale answer (the user changed the sort or filter meanwhile) is dropped."""

    page_loaded = Signal()
    load_failed = Signal(str)

    def __init__(self, client: ApiClient, parent: Any = None, page_size: int = PAGE_SIZE) -> None:
        super().__init__(parent)
        self._client = client
        self._page_size = page_size
        self._rows: list[BookmarkSummary] = []
        self._total = 0
        self._cursor: str | None = None
        self._more = True
        self._loading = False
        self._gen = 0
        self._query: dict[str, Any] = {}
        self._sort, self._desc = "id", False
        self._bold = QFont()
        self._bold.setBold(True)

    # -- query --------------------------------------------------------------------------

    @property
    def total(self) -> int:
        return self._total

    @property
    def loading(self) -> bool:
        return self._loading

    def set_query(self, **query: Any) -> None:
        self._query = {k: v for k, v in query.items() if v not in (None, "")}
        self.reload()

    def reload(self) -> None:
        self.beginResetModel()
        self._rows = []
        self._cursor = None
        self._more = True
        self._gen += 1
        self._loading = False
        self.endResetModel()
        self._fetch()

    def _fetch(self) -> None:
        if self._loading:
            return
        self._loading = True
        gen = self._gen
        params = {
            **self._query,
            "sort": self._sort,
            "desc": self._desc or None,
            "cursor": self._cursor,
            "limit": self._page_size,
        }
        run_async(
            lambda: self._client.bookmarks(**params),
            lambda page: self._arrived(gen, page),
            lambda exc: self._failed(gen, exc),
        )

    def _arrived(self, gen: int, page: Any) -> None:
        if gen != self._gen:
            return
        self._loading = False
        self._total = page.total or 0
        self._cursor = page.next_cursor
        self._more = page.next_cursor is not None
        if page.items:
            first = len(self._rows)
            self.beginInsertRows(QModelIndex(), first, first + len(page.items) - 1)
            self._rows.extend(page.items)
            self.endInsertRows()
        self.page_loaded.emit()

    def _failed(self, gen: int, exc: Exception) -> None:
        if gen == self._gen:
            self._loading = False
            self.load_failed.emit(str(exc))

    # -- lazy loading -------------------------------------------------------------------

    def canFetchMore(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> bool:  # noqa: B008
        return not parent.isValid() and self._more and not self._loading

    def fetchMore(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> None:  # noqa: B008
        if self.canFetchMore(parent):
            self._fetch()

    # -- single-row updates -------------------------------------------------------------

    def row_of(self, bookmark_id: int) -> int | None:
        for i, r in enumerate(self._rows):
            if r.id == bookmark_id:
                return i
        return None

    def refresh_bookmark(self, bookmark_id: int) -> None:
        """Re-read one bookmark (after an event or an action) and update its row in place."""
        if self.row_of(bookmark_id) is None:
            return
        gen = self._gen

        def done(out: BookmarkOut) -> None:
            if gen != self._gen:
                return
            row = self.row_of(bookmark_id)
            if row is not None:
                self._rows[row] = summary_from_out(out, self._rows[row])
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

        run_async(lambda: self._client.bookmark(bookmark_id), done, lambda _e: None)

    # -- QAbstractTableModel ------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def summary(self, row: int) -> BookmarkSummary:
        return self._rows[row]

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][0]
        return None

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        b = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                "",
                b.name,
                fmt_time(b.last_changed_at),
                fmt_time(b.last_checked_at),
                fmt_time(b.next_due_at),
                fmt_interval(b.interval_s),
                str(b.consecutive_errors) if b.consecutive_errors else "",
                ", ".join(b.keyword_hits),
            )[col]
        if role == Qt.ItemDataRole.DecorationRole and col == COL_STATUS:
            return status_icon(b.status.value)
        if role == Qt.ItemDataRole.FontRole and b.unread:
            return self._bold
        if role == Qt.ItemDataRole.ForegroundRole and not b.enabled:
            return QBrush(QColor("#888888"))
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{b.url}\n{b.status.value}" + (
                f"\nkeywords: {', '.join(b.keyword_hits)}" if b.keyword_hits else ""
            )
        if role == SummaryRole:
            return b
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        key = COLUMNS[column][1]
        if key is None:
            return
        self._sort, self._desc = key, order == Qt.SortOrder.DescendingOrder
        self.reload()


# -- folder tree ------------------------------------------------------------------------

BUILTINS: list[tuple[str, str]] = [
    ("all", "All bookmarks"),
    ("changed_today", "Changed today"),
    ("unread", "Unread"),
    ("errors", "Errors"),
    ("needs_login", "Needs login"),
    ("keyword_hits", "Keyword hits"),
]
KeyRole = Qt.ItemDataRole.UserRole + 2


def builtin_query(key: str, now: datetime | None = None) -> dict[str, Any]:
    if key == "changed_today":
        since = (now or datetime.now(UTC)) - timedelta(hours=24)
        return {"changed_since": since.strftime(ISO_FMT)}
    return {
        "all": {},
        "unread": {"unread": True},
        "errors": {"status": "error"},
        "needs_login": {"status": "needs_login"},
        "keyword_hits": {"keyword_hits": True},
    }[key]


class FolderTree(QTreeWidget):
    """Built-in virtual folders first, then the real folder hierarchy."""

    query_selected = Signal(dict)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setObjectName("folderTree")
        self._builtin_items: dict[str, QTreeWidgetItem] = {}
        self._folder_items: dict[int, QTreeWidgetItem] = {}
        self._bold = QFont()
        self._bold.setBold(True)
        for key, label in BUILTINS:
            item = QTreeWidgetItem([label])
            item.setData(0, KeyRole, key)
            self.addTopLevelItem(item)
            self._builtin_items[key] = item
        self._folders_root = QTreeWidgetItem(["Folders"])
        self._folders_root.setFlags(self._folders_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.addTopLevelItem(self._folders_root)
        self.currentItemChanged.connect(self._changed)
        self.setCurrentItem(self._builtin_items["all"])

    def _changed(self, current: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None) -> None:
        if current is not None:
            self.query_selected.emit(self.query_for(current))

    def query_for(self, item: QTreeWidgetItem) -> dict[str, Any]:
        key = item.data(0, KeyRole)
        if isinstance(key, str) and key in self._builtin_items:
            return builtin_query(key)
        if isinstance(key, int):
            return {"folder": key}
        return {}

    def load(self, folders: list[FolderOut], counts: BookmarkCounts | None) -> None:
        """Rebuild the folder branch and refresh counts, keeping the current selection."""
        current = self.currentItem()
        keep = current.data(0, KeyRole) if current is not None else "all"
        self.blockSignals(True)
        for child in self._folders_root.takeChildren():
            del child
        self._folder_items = {}
        for f in sorted(folders, key=lambda f: (f.sort_order, f.name.lower())):
            if f.is_virtual:
                continue
            item = QTreeWidgetItem([f.name])
            item.setData(0, KeyRole, f.id)
            self._folder_items[f.id] = item
        for f in folders:
            item = self._folder_items.get(f.id)
            if item is None:
                continue
            parent = self._folder_items.get(f.parent_id) if f.parent_id is not None else None
            (parent or self._folders_root).addChild(item)
        self._folders_root.setExpanded(True)
        if counts is not None:
            for key, label in BUILTINS:
                n = {
                    "all": counts.total,
                    "changed_today": counts.changed_today,
                    "unread": counts.unread,
                    "errors": counts.errors,
                    "needs_login": counts.needs_login,
                    "keyword_hits": counts.keyword_hits,
                }[key]
                item = self._builtin_items[key]
                item.setText(0, f"{label} ({n})" if key != "all" or n else label)
                item.setFont(
                    0, self._bold if key in ("unread", "errors", "keyword_hits") and n else QFont()
                )
            for fid, item in self._folder_items.items():
                c = counts.by_folder.get(fid)
                name = item.text(0).split(" (")[0]
                item.setText(0, f"{name} ({c.unread})" if c and c.unread else name)
                item.setFont(0, self._bold if c and c.unread else QFont())
        target = (
            self._builtin_items.get(keep) if isinstance(keep, str) else self._folder_items.get(keep)
        )
        self.blockSignals(False)
        if target is not None and target is not self.currentItem():
            self.setCurrentItem(target)

    def selected_folder_id(self) -> int | None:
        item = self.currentItem()
        key = item.data(0, KeyRole) if item is not None else None
        return key if isinstance(key, int) else None

    def folder_ids(self) -> list[int]:
        return list(self._folder_items)
