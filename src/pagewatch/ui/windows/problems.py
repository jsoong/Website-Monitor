"""The Problems window: bookmarks that are failing or need a sign-in, and actions that gave up
(spec: *Desktop UI → Other screens*; *Actions → Execution rules*: "Failed jobs appear in the UI's
Problems list with the last error").

It reads ``GET /problems``. A problem goes away when its cause does: a bookmark when a check
succeeds or when it is opened (marked read), a failed action when its change is read. A failed
action can also be sent round again (**Retry**: fresh set of attempts) or removed (**Dismiss**);
those two are the only things this window changes, through ``/action-jobs``. The window refreshes on
the engine's ``problem`` events and when asked.
"""

from __future__ import annotations

import webbrowser
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from pagewatch.models import ProblemOut, ProblemsOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

REFRESH_DEBOUNCE_MS = 300
COLUMNS = ["Problem", "Bookmark", "What went wrong", "When", "Tries"]
COL_KIND, COL_NAME, COL_WHY, COL_WHEN, COL_TRIES = range(5)

# What a check's failure code means, in words. The raw code stays in the tooltip.
REASONS = {
    "timeout": "The page did not answer in time",
    "dns": "The address could not be found (DNS)",
    "tls": "The secure connection failed (certificate or TLS)",
    "connection": "The connection was refused or dropped",
    "too_large": "The page is larger than the size limit",
    "browser": "The browser could not load the page",
    "parse": "The content could not be read",
    "needs login": "The site wants you to sign in again",
}
KINDS = {
    "bookmark_error": "Failing",
    "needs_login": "Needs sign-in",
    "action_failed": "Action failed",
}


def explain(problem: ProblemOut) -> str:
    """A sentence for the list; actions already carry their own error text."""
    if problem.kind == "action_failed":
        return problem.message
    code = problem.message
    if code in REASONS:
        return REASONS[code]
    if code.startswith("http_") and code[5:].isdigit():
        return f"The server answered HTTP {code[5:]}"
    return code


def when_text(iso_text: str | None) -> str:
    """UTC ISO time as the user's local ``YYYY-MM-DD HH:MM``."""
    if not iso_text:
        return ""
    try:
        return (
            datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except ValueError:
        return iso_text


class ProblemsWindow(QDialog):
    """Not modal: it stays open beside the main window and keeps itself current."""

    show_bookmark = Signal(int, str)  # bookmark id, its URL: select it in the main list
    count_changed = Signal(int)

    def __init__(self, client: ApiClient, parent: Any = None) -> None:
        super().__init__(parent)
        self._client = client
        self._generation = 0
        self._items: list[ProblemOut] = []
        self.total = 0
        self.setWindowTitle("Problems")
        self.setModal(False)
        self.resize(900, 420)

        self.summary = QLabel("")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("problemList")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_WHY, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(COL_KIND, 110)
        self.table.setColumnWidth(COL_NAME, 220)
        self.table.setColumnWidth(COL_WHEN, 130)
        self.table.setColumnWidth(COL_TRIES, 50)
        self.empty = QLabel("No problems. Everything that can be checked is being checked.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message = QLabel("")

        self.btn_check = QPushButton("Check now")
        self.btn_show = QPushButton("Show bookmark")
        self.btn_open = QPushButton("Open URL")
        self.btn_copy = QPushButton("Copy error")
        self.btn_retry = QPushButton("Retry")
        self.btn_dismiss = QPushButton("Dismiss")
        self.btn_refresh = QPushButton("Refresh")
        close = QPushButton("Close")
        self.btn_check.setToolTip("Check the selected bookmark again now")
        self.btn_retry.setToolTip("Run the failed action again, with a fresh set of attempts")
        self.btn_dismiss.setToolTip("Remove the failed action from this list without retrying it")
        self.btn_show.setToolTip("Select it in the main window's list")
        self.btn_check.clicked.connect(self.check_selected)
        self.btn_show.clicked.connect(self.show_selected)
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_copy.clicked.connect(self.copy_selected)
        self.btn_retry.clicked.connect(self.retry_selected)
        self.btn_dismiss.clicked.connect(self.dismiss_selected)
        self.btn_refresh.clicked.connect(self.refresh)
        close.clicked.connect(self.close)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self.show_selected())

        row = QHBoxLayout()
        for b in (self.btn_check, self.btn_show, self.btn_open, self.btn_copy):
            row.addWidget(b)
        row.addSpacing(12)
        row.addWidget(self.btn_retry)
        row.addWidget(self.btn_dismiss)
        row.addStretch(1)
        row.addWidget(self.btn_refresh)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty)
        layout.addWidget(self.message)
        layout.addLayout(row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(REFRESH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh)
        self._sync_buttons()
        self.refresh()

    # -- data ---------------------------------------------------------------------------

    def schedule_refresh(self) -> None:
        """Engine events come in bursts; one refresh follows the last of them."""
        self._debounce.start()

    def refresh(self) -> None:
        self._generation += 1
        gen = self._generation

        def done(out: ProblemsOut) -> None:
            if gen == self._generation:  # a newer request supersedes this answer
                self._show(out)

        run_async(self._client.problems, done, lambda e: self.say(f"Could not load: {e}"))

    def _show(self, out: ProblemsOut) -> None:
        keep = self.selected_problem()
        self._items = list(out.items)
        self.total = out.total
        self.table.setRowCount(len(self._items))
        for r, p in enumerate(self._items):
            cells = [
                KINDS[p.kind] + (f" ({p.action_type})" if p.action_type else ""),
                p.bookmark_name,
                explain(p),
                when_text(p.occurred_at),
                str(p.attempts if p.attempts is not None else p.consecutive_errors or ""),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{p.url}\n{p.message}" if c in (COL_NAME, COL_WHY) else "")
                self.table.setItem(r, c, item)
        if keep is not None:  # keep the selection across refreshes
            for r, p in enumerate(self._items):
                if (p.kind, p.bookmark_id, p.job_id) == (keep.kind, keep.bookmark_id, keep.job_id):
                    self.table.selectRow(r)
                    break
        shown = len(self._items)
        self.summary.setText(
            "" if not self.total
            else f"{self.total} problem{'s' if self.total != 1 else ''}"
            + (f" (showing the newest {shown})" if shown < self.total else "")
        )  # fmt: skip
        self.table.setVisible(bool(shown))
        self.empty.setVisible(not shown)
        self._sync_buttons()
        self.count_changed.emit(self.total)

    # -- selection and actions ----------------------------------------------------------

    def say(self, text: str) -> None:
        self.message.setText(text)

    def selected_problem(self) -> ProblemOut | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._items[rows[0].row()] if rows and rows[0].row() < len(self._items) else None

    def _sync_buttons(self) -> None:
        p = self.selected_problem()
        self.btn_check.setEnabled(p is not None and p.kind != "action_failed")
        for b in (self.btn_show, self.btn_open, self.btn_copy):
            b.setEnabled(p is not None)
        for b in (self.btn_retry, self.btn_dismiss):
            b.setEnabled(self._failed_job() is not None)

    def _failed_job(self) -> ProblemOut | None:
        """The selected problem, if it is a failed action (the only kind with a job to retry)."""
        p = self.selected_problem()
        return p if p is not None and p.kind == "action_failed" and p.job_id is not None else None

    def check_selected(self) -> None:
        p = self.selected_problem()
        if p is None or p.kind == "action_failed":
            return
        run_async(
            lambda: self._client.check([p.bookmark_id], force=True),
            lambda _n: self.say(f"Checking {p.bookmark_name}"),
            lambda e: self.say(str(e)),
        )

    def retry_selected(self) -> None:
        p = self._failed_job()
        if p is None or p.job_id is None:
            return
        job_id = p.job_id
        run_async(
            lambda: self._client.retry_action_job(job_id),
            lambda _job: self._after_job(f"Queued again: {p.bookmark_name} ({p.action_type})"),
            lambda e: self._after_job(str(e)),  # e.g. it was dismissed elsewhere: the list is stale
        )

    def dismiss_selected(self) -> None:
        p = self._failed_job()
        if p is None or p.job_id is None:
            return
        job_id = p.job_id
        run_async(
            lambda: self._client.dismiss_action_job(job_id),
            lambda _none: self._after_job(f"Dismissed: {p.bookmark_name} ({p.action_type})"),
            lambda e: self._after_job(str(e)),
        )

    def _after_job(self, text: str) -> None:
        self.say(text)
        self.refresh()

    def show_selected(self) -> None:
        p = self.selected_problem()
        if p is not None:
            self.show_bookmark.emit(p.bookmark_id, p.url)

    def open_selected(self) -> None:
        p = self.selected_problem()
        if p is not None:
            self._open_external(p.url)

    def _open_external(self, url: str) -> None:  # a seam so tests do not open a browser
        webbrowser.open(url)

    def copy_selected(self) -> None:
        p = self.selected_problem()
        if p is None:
            return
        QGuiApplication.clipboard().setText(f"{p.bookmark_name}\n{p.url}\n{p.message}")
        self.say("Copied")

    def closeEvent(self, event: Any) -> None:
        self._debounce.stop()
        super().closeEvent(event)
