"""Import and export (spec: Other screens → Import/Export)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from pagewatch.models import FolderOut, ImportResult
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

EXPORT_FORMATS = [
    ("PageWatch configuration (JSON)", "json", "json"),
    ("Feeds as OPML", "opml", "opml"),
    ("List as CSV (opens in Excel)", "csv", "csv"),
]
IMPORT_FILTER = "Bookmark files (*.html *.htm *.csv *.xlsx *.json *.txt);;All files (*)"


def result_text(r: ImportResult) -> str:
    head = "Would import" if r.dry_run else "Imported"
    lines = [
        f"{head} {r.created} bookmark(s) from {r.format}; "
        f"{r.skipped_duplicates} already existed; {r.folders_created} folder(s) "
        f"{'would be ' if r.dry_run else ''}created."
    ]
    lines += [f"  row {e.row}: {e.message}" for e in r.errors[:50]]
    if len(r.errors) > 50:
        lines.append(f"  … and {len(r.errors) - 50} more problems")
    return "\n".join(lines)


class ImportExportDialog(QDialog):
    #: bookmarks or folders were created: the main window should reload
    changed = Signal()

    def __init__(
        self,
        client: ApiClient,
        folders: list[FolderOut],
        *,
        selected_ids: list[int] | None = None,
        folder_id: int | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._selected = list(selected_ids or [])
        self.setWindowTitle("Import / Export")
        self.resize(560, 460)

        self.import_path_edit = QLineEdit()
        self.import_path_edit.setObjectName("importPath")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_import)
        self.into = QComboBox()
        self.into.setObjectName("importInto")
        self.into.addItem("(top level)", None)
        for f in sorted(folders, key=lambda f: f.name.lower()):
            if not f.is_virtual:
                self.into.addItem(f.name, f.id)
        self.into.setCurrentIndex(max(0, self.into.findData(folder_id)))
        self.dry_run = QCheckBox("Dry run: only report what would happen")
        self.import_button = QPushButton("Import")
        self.import_button.setObjectName("importButton")
        self.import_button.clicked.connect(lambda: self.start_import())
        self.report = QPlainTextEdit()
        self.report.setObjectName("importReport")
        self.report.setReadOnly(True)
        self.report.setMaximumHeight(130)

        imp = QGroupBox("Import")
        row = QHBoxLayout()
        row.addWidget(self.import_path_edit, 1)
        row.addWidget(browse)
        form = QFormLayout(imp)
        form.addRow("File", row)
        form.addRow(
            QLabel("Browser bookmarks (HTML), a list of URLs, CSV or XLSX with name, url, "
                   "folder, interval, keywords columns, or a PageWatch export.")
        )  # fmt: skip
        form.addRow("Into folder", self.into)
        form.addRow(self.dry_run)
        form.addRow(self.import_button)
        form.addRow(self.report)

        self.format = QComboBox()
        self.format.setObjectName("exportFormat")
        for label, fmt, _ in EXPORT_FORMATS:
            self.format.addItem(label, fmt)
        self.scope = QComboBox()
        self.scope.setObjectName("exportScope")
        self.scope.addItem("Everything", ("all", None))
        if folder_id is not None:
            self.scope.addItem("The selected folder", ("folder", folder_id))
        if self._selected:
            self.scope.addItem(f"The {len(self._selected)} selected bookmark(s)", ("ids", None))
        self.export_button = QPushButton("Export…")
        self.export_button.setObjectName("exportButton")
        self.export_button.clicked.connect(self._browse_export)
        self.export_status = QLabel("")
        self.export_status.setObjectName("exportStatus")
        self.export_status.setWordWrap(True)
        exp = QGroupBox("Export")
        eform = QFormLayout(exp)
        eform.addRow("Format", self.format)
        eform.addRow("What", self.scope)
        eform.addRow(self.export_button)
        eform.addRow(self.export_status)

        layout = QVBoxLayout(self)
        layout.addWidget(imp)
        layout.addWidget(exp)

    # -- import -------------------------------------------------------------------------

    def _browse_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import", "", IMPORT_FILTER)
        if path:
            self.import_path_edit.setText(path)

    def start_import(self, path: str | None = None, dry_run: bool | None = None) -> None:
        chosen = Path(path or self.import_path_edit.text().strip())
        if not chosen.name:
            self.report.setPlainText("Choose a file first.")
            return
        dry = self.dry_run.isChecked() if dry_run is None else dry_run
        folder_id = self.into.currentData()
        self.import_button.setEnabled(False)
        self.report.setPlainText("Importing…")

        def work() -> ImportResult:
            data = chosen.read_bytes()
            body: dict[str, Any] = {"filename": chosen.name, "dry_run": dry}
            if folder_id is not None:
                body["folder_id"] = folder_id
            if data[:2] == b"PK":  # a workbook
                body["content_base64"] = base64.b64encode(data).decode("ascii")
            else:
                body["content"] = data.decode("utf-8-sig", errors="replace")
            return self._client.import_bookmarks(body)

        def done(res: ImportResult) -> None:
            self.import_button.setEnabled(True)
            self.report.setPlainText(result_text(res))
            if not res.dry_run and (res.created or res.folders_created):
                self.changed.emit()

        def failed(exc: Exception) -> None:
            self.import_button.setEnabled(True)
            self.report.setPlainText(f"Import failed: {str(exc).split(': ', 1)[-1]}")

        run_async(work, done, failed)

    # -- export -------------------------------------------------------------------------

    def _browse_export(self) -> None:
        fmt = self.format.currentData()
        ext = next(e for _, f, e in EXPORT_FORMATS if f == fmt)
        path, _ = QFileDialog.getSaveFileName(self, "Export", f"pagewatch-export.{ext}")
        if path:
            self.start_export(path)

    def start_export(self, path: str) -> None:
        fmt = str(self.format.currentData())
        kind, value = self.scope.currentData()
        folder = value if kind == "folder" else None
        ids = self._selected if kind == "ids" else None
        self.export_button.setEnabled(False)
        self.export_status.setText("Exporting…")

        def work() -> int:
            data = self._client.export_bookmarks(fmt, folder=folder, ids=ids)
            Path(path).write_bytes(data)
            return len(data)

        def done(n: int) -> None:
            self.export_button.setEnabled(True)
            self.export_status.setText(f"Saved {n:,} bytes to {path}")

        def failed(exc: Exception) -> None:
            self.export_button.setEnabled(True)
            self.export_status.setText(f"Export failed: {str(exc).split(': ', 1)[-1]}")

        run_async(work, done, failed)
