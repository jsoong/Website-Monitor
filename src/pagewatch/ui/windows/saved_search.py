"""Create or edit a saved search (a virtual folder): a name and the criteria a bookmark must meet."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from pagewatch.models import FolderOut, VirtualQuery

STATUSES = ("new", "ok", "changed", "error", "needs_login", "disabled")
SOURCES = ("html", "feed", "pdf", "docx", "xlsx", "ftp", "file", "folder", "image", "binary",
           "records")  # fmt: skip
METHODS = ("auto", "static", "browser", "screenshot")
TRI = [("any", None), ("yes", True), ("no", False)]


def _tri() -> QComboBox:
    box = QComboBox()
    for label, value in TRI:
        box.addItem(label, value)
    return box


def _choice(values: tuple[str, ...]) -> QComboBox:
    box = QComboBox()
    box.addItem("any", None)
    for v in values:
        box.addItem(v, v)
    return box


class SavedSearchDialog(QDialog):
    """``name()`` and ``query()`` are what ``POST /folders`` (``is_virtual``) wants."""

    def __init__(
        self,
        folders: list[FolderOut],
        *,
        name: str = "",
        query: dict[str, Any] | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        q = VirtualQuery.model_validate(query or {})
        self.setWindowTitle("Saved search")
        self.name_edit = QLineEdit(name)
        self.name_edit.setObjectName("savedName")
        self.text = QLineEdit(q.q or "")
        self.text.setPlaceholderText("part of the name or the URL")
        self.status = _choice(STATUSES)
        self.unread = _tri()
        self.enabled = _tri()
        self.hits = QCheckBox("Only bookmarks with unread keyword hits")
        self.folder = QComboBox()
        self.folder.addItem("anywhere", None)
        for f in sorted(folders, key=lambda f: f.name.lower()):
            if not f.is_virtual:
                self.folder.addItem(f.name, f.id)
        self.within = QSpinBox()
        self.within.setRange(0, 24 * 365)
        self.within.setSpecialValueText("any time")
        self.within.setSuffix(" hours")
        self.source = _choice(SOURCES)
        self.method = _choice(METHODS)
        self.plugin = QLineEdit(q.plugin or "")
        self.errors = QSpinBox()
        self.errors.setRange(0, 1000)
        self.errors.setSpecialValueText("any")
        self.errors.setSuffix(" or more")
        self.error = QLabel("")
        self.error.setStyleSheet("color: #c62828")

        self._set(self.status, q.status.value if q.status else None)
        self._set(self.unread, q.unread)
        self._set(self.enabled, q.enabled)
        self.hits.setChecked(bool(q.keyword_hits))
        self._set(self.folder, q.folder)
        self.within.setValue(int(q.changed_within_h or 0))
        self._set(self.source, q.source_type.value if q.source_type else None)
        self._set(self.method, q.check_method.value if q.check_method else None)
        self.errors.setValue(q.min_errors or 0)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Contains", self.text)
        form.addRow("Status", self.status)
        form.addRow("Unread", self.unread)
        form.addRow("Enabled", self.enabled)
        form.addRow(self.hits)
        form.addRow("In folder", self.folder)
        form.addRow("Changed within", self.within)
        form.addRow("Source type", self.source)
        form.addRow("Check method", self.method)
        form.addRow("Plugin", self.plugin)
        form.addRow("Consecutive errors", self.errors)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    @staticmethod
    def _set(box: QComboBox, value: Any) -> None:
        box.setCurrentIndex(max(0, box.findData(value)))

    def name(self) -> str:
        return self.name_edit.text().strip()

    def query(self) -> dict[str, Any]:
        """Only the criteria that were set."""
        out: dict[str, Any] = {}
        if self.text.text().strip():
            out["q"] = self.text.text().strip()
        for key, box in (("status", self.status), ("unread", self.unread),
                         ("enabled", self.enabled), ("folder", self.folder),
                         ("source_type", self.source), ("check_method", self.method)):  # fmt: skip
            value = box.currentData()
            if value is not None:
                out[key] = value
        if self.hits.isChecked():
            out["keyword_hits"] = True
        if self.within.value():
            out["changed_within_h"] = float(self.within.value())
        if self.plugin.text().strip():
            out["plugin"] = self.plugin.text().strip()
        if self.errors.value():
            out["min_errors"] = self.errors.value()
        return out

    def _save(self) -> None:
        if not self.name():
            self.error.setText("Give the search a name.")
        elif not self.query():
            self.error.setText("Choose at least one criterion.")
        else:
            self.error.setText("")
            self.accept()
