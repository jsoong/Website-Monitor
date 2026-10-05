"""Review the automatic filters proposed after flagging a change as a false positive."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from pagewatch.models import FalsePositiveOut


class FalsePositiveDialog(QDialog):
    """Nothing is saved until the user confirms: ``patch()`` is the ``PATCH`` body to apply."""

    def __init__(self, result: FalsePositiveOut, parent: Any = None) -> None:
        super().__init__(parent)
        self.result = result
        self.setWindowTitle("Flagged as a false positive")
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        if result.proposals:
            verdict = (
                "Together these filters remove the change."
                if result.resolves_all
                else f"These filters leave {result.remaining_changed_blocks} changed block(s)."
            )
            layout.addWidget(
                QLabel(f"Proposed ignore filters. {verdict} Untick any you do not want.")
            )
        else:
            layout.addWidget(
                QLabel(
                    "No filter could be proposed for this change (no page structure to point at)."
                )
            )
        self.list = QListWidget()
        for p in result.proposals:
            mark = "✓ removes the change" if p.verified else "partial"
            item = QListWidgetItem(
                f"[{mark}] {p.explanation}\n    {p.example_old[:60]}  →  {p.example_new[:60]}"
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(bool(result.proposals))
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_rules(self) -> list[dict[str, Any]]:
        return [
            self.result.proposals[i].rule.model_dump(mode="json", exclude_defaults=True)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def patch(self) -> dict[str, Any]:
        """The existing ignore rules plus the ticked proposals."""
        everything = self.result.patch["filter"]["ignore"]
        existing = everything[: len(everything) - len(self.result.proposals)]
        return {"filter": {"ignore": [*existing, *self.selected_rules()]}}
