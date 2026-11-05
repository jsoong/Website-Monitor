"""Problems: action jobs that failed after their retries (spec: "Failed jobs appear in the UI's
Problems list with the last error"). Retry puts a job back in the queue with fresh attempts;
Dismiss removes it from the list."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from pagewatch.models import ActionJobOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

COLUMNS = ["When", "Bookmark", "Action", "Attempts", "Last error"]


class FailedActionsDialog(QDialog):
    loaded = Signal(list)
    message = Signal(str)

    def __init__(self, client: ApiClient, parent: Any = None) -> None:
        super().__init__(parent)
        self._client = client
        self._jobs: list[ActionJobOut] = []
        self.setWindowTitle("Problems: failed actions")
        self.resize(820, 380)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("failedActions")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.status = QLabel("")
        self.retry = QPushButton("Retry")
        self.dismiss = QPushButton("Dismiss")
        self.refresh_button = QPushButton("Refresh")
        close = QPushButton("Close")
        buttons = QHBoxLayout()
        for b in (self.retry, self.dismiss, self.refresh_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        self.loaded.connect(self._fill)
        self.message.connect(self.status.setText)
        self.retry.clicked.connect(self._retry)
        self.dismiss.clicked.connect(self._dismiss)
        self.refresh_button.clicked.connect(self.refresh)
        close.clicked.connect(self.accept)
        self.refresh()

    def refresh(self) -> None:
        run_async(
            self._client.action_jobs,
            lambda jobs: self.loaded.emit(jobs),
            lambda e: self.message.emit(f"Could not load the list: {e}"),
        )

    def _fill(self, jobs: list[ActionJobOut]) -> None:
        self._jobs = jobs
        self.table.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            when = (job.detected_at or "").replace("T", " ")[:16]
            cells = [when, job.bookmark_name or f"#{job.bookmark_id}", job.action_type,
                     str(job.attempts), job.last_error or ""]  # fmt: skip
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(Qt.ItemDataRole.UserRole, job.id)
                self.table.setItem(r, c, item)
        self.table.resizeRowsToContents()
        self.status.setText("" if jobs else "No failed actions.")
        self.retry.setEnabled(bool(jobs))
        self.dismiss.setEnabled(bool(jobs))

    def selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._jobs[r].id for r in rows if r < len(self._jobs)]

    def _each(self, fn: Any, done: str) -> None:
        ids = self.selected_ids()
        if not ids:
            self.status.setText("Select one or more rows first.")
            return

        def work() -> int:
            for job_id in ids:
                fn(job_id)
            return len(ids)

        run_async(
            work,
            lambda n: (self.message.emit(f"{done} {n}."), self.refresh()),
            lambda e: self.message.emit(str(e)),
        )

    def _retry(self) -> None:
        self._each(self._client.retry_action_job, "Queued again:")

    def _dismiss(self) -> None:
        self._each(self._client.dismiss_action_job, "Dismissed:")
