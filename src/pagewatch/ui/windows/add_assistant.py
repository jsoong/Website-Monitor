"""Add-bookmark assistant: paste a URL, see what PageWatch makes of it (type, chosen method,
a rendered preview), let it fetch twice and pre-propose ignore filters for anything that already
differs (clocks, tokens), optionally watch only a region, then save."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from pagewatch.models import BookmarkOut, FolderOut, PreviewOut, ProposalOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.viewer import HtmlView
from pagewatch.ui.workers import run_async

INTERVALS = [
    ("5 minutes", 300), ("15 minutes", 900), ("30 minutes", 1800), ("1 hour", 3600),
    ("6 hours", 21600), ("1 day", 86400), ("1 week", 604800),
]  # fmt: skip


class AddBookmarkDialog(QDialog):
    created = Signal(object)  # BookmarkOut
    previewed = Signal(object)  # PreviewOut

    def __init__(
        self,
        client: ApiClient,
        folders: list[FolderOut],
        default_folder_id: int | None = None,
        parent: Any = None,
        *,
        samples: int = 2,
        gap_s: float = 5.0,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._samples, self._gap_s = samples, gap_s
        self._proposals: list[ProposalOut] = []
        self.preview_out: PreviewOut | None = None
        self.setWindowTitle("Add bookmark")
        self.resize(760, 640)

        self.url = QLineEdit()
        self.url.setPlaceholderText("Paste a URL: https://example.com/page")
        self.preview_btn = QPushButton("Preview")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.web: HtmlView | None = None
        self.proposal_list = QListWidget()
        self.proposal_list.setMaximumHeight(110)
        self.whole_page = QRadioButton("Watch the whole page")
        self.whole_page.setChecked(True)
        self.region = QRadioButton("Watch only this CSS selector:")
        self.selector = QLineEdit()
        self.selector.setPlaceholderText("#price, .listing, main article")
        self.name = QLineEdit()
        self.folder = QComboBox()
        self.folder.addItem("(no folder)", None)
        for f in sorted(folders, key=lambda f: f.name.lower()):
            if not f.is_virtual:
                self.folder.addItem(f.name, f.id)
        self.folder.setCurrentIndex(max(0, self.folder.findData(default_folder_id)))
        self.interval = QComboBox()
        for label, secs in INTERVALS:
            self.interval.addItem(label, secs)
        self.interval.setCurrentIndex(3)
        self.private = QCheckBox("Private alerts (never include page content)")
        self.error = QLabel("")
        self.error.setStyleSheet("color: #c62828")
        self.error.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(self.url, 1)
        top.addWidget(self.preview_btn)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Folder", self.folder)
        form.addRow("Check every", self.interval)
        form.addRow(self.private)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_btn = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_btn.setEnabled(False)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)

        self.layout_ = QVBoxLayout(self)
        self.layout_.addLayout(top)
        self.layout_.addWidget(self.status)
        self.preview_slot = QVBoxLayout()
        self.layout_.addLayout(self.preview_slot, 1)
        self.layout_.addWidget(
            QLabel("Ignore filters proposed for what already differs between two fetches:")
        )
        self.layout_.addWidget(self.proposal_list)
        self.layout_.addWidget(self.whole_page)
        region_row = QHBoxLayout()
        region_row.addWidget(self.region)
        region_row.addWidget(self.selector, 1)
        self.layout_.addLayout(region_row)
        self.layout_.addLayout(form)
        self.layout_.addWidget(self.error)
        self.layout_.addWidget(self.buttons)

        self.preview_btn.clicked.connect(self.preview)
        self.url.returnPressed.connect(self.preview)
        self.selector.textChanged.connect(lambda t: self.region.setChecked(bool(t)))

    # -- preview ------------------------------------------------------------------------

    def preview(self) -> None:
        url = self.url.text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
            self.url.setText(url)
        self.status.setText(
            f"Fetching {self._samples} time{'s' if self._samples != 1 else ''}"
            f"{f', {self._gap_s:g} s apart' if self._samples > 1 else ''}..."
        )
        self.preview_btn.setEnabled(False)
        self.error.setText("")
        run_async(
            lambda: self._client.preview(url, samples=self._samples, gap_s=self._gap_s),
            self._show_preview,
            self._preview_failed,
        )

    def _preview_failed(self, exc: Exception) -> None:
        self.preview_btn.setEnabled(True)
        self.status.setText("")
        self.error.setText(str(exc).split(": ", 1)[-1])

    def _show_preview(self, out: PreviewOut) -> None:
        self.preview_btn.setEnabled(True)
        self.preview_out = out
        if out.error:
            self.status.setText("")
            self.error.setText(f"Could not fetch the page: {out.error}")
            self.save_btn.setEnabled(False)
            self.previewed.emit(out)
            return
        kinds = {
            "page": "a web page",
            "js-app": "a JavaScript app",
            "feed": "a feed",
            "pdf": "a PDF",
        }
        note = (
            " The page needs a browser to render: PageWatch will switch to it automatically."
            if out.js_app
            else ""
        )
        self.status.setText(
            f"Detected {kinds.get(out.kind, out.kind)}; method: {out.method}; {out.words:,} words in "
            f"{out.blocks:,} blocks.{note}"
        )
        if self.web is None:
            self.web = HtmlView()
            self.web.setMinimumHeight(220)
            self.preview_slot.addWidget(self.web)
        self.web.set_document(out.html)
        self.proposal_list.clear()
        self._proposals = list(out.proposals)
        for p in self._proposals:
            item = QListWidgetItem(
                f"{'✓' if p.verified else '?'} {p.explanation}: {p.example_old[:50]} → {p.example_new[:50]}"
            )
            item.setFlags(item.flags() | item.flags().ItemIsUserCheckable)
            item.setCheckState(
                item.checkState().Checked if p.verified else item.checkState().Unchecked
            )
            self.proposal_list.addItem(item)
        if not self.name.text():
            self.name.setText(self._guess_name(out))
        self.save_btn.setEnabled(True)
        self.previewed.emit(out)

    @staticmethod
    def _guess_name(out: PreviewOut) -> str:
        from urllib.parse import urlsplit

        host = urlsplit(out.final_url).hostname or out.final_url
        return host.removeprefix("www.")

    # -- save ---------------------------------------------------------------------------

    def accepted_rules(self) -> list[dict[str, Any]]:
        return [
            self._proposals[i].rule.model_dump(mode="json", exclude_defaults=True)
            for i in range(self.proposal_list.count())
            if self.proposal_list.item(i).checkState()
            == self.proposal_list.item(i).checkState().Checked
        ]

    def build_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "url": self.url.text().strip(),
            "schedule": {"interval_s": int(self.interval.currentData())},
            "folder_id": self.folder.currentData(),
        }
        if self.name.text().strip():
            body["name"] = self.name.text().strip()
        filt: dict[str, Any] = {}
        rules = self.accepted_rules()
        if rules:
            filt["ignore"] = rules
        if self.region.isChecked() and self.selector.text().strip():
            filt["watch"] = [{"type": "selector", "selector": self.selector.text().strip()}]
        if filt:
            body["filter"] = filt
        if self.private.isChecked():
            body["actions"] = {"actions": [{"type": "toast"}], "alert_privacy": "private"}
        if self.preview_out is not None and self.preview_out.js_app:
            body["check_method"] = "auto"  # the engine switches to the browser on its own
        return body

    def save(self) -> None:
        body = self.build_body()
        self.save_btn.setEnabled(False)

        def done(out: BookmarkOut) -> None:
            self.created.emit(out)
            self.accept()

        def failed(exc: Exception) -> None:
            self.save_btn.setEnabled(True)
            self.error.setText(str(exc).split(": ", 1)[-1])

        run_async(lambda: self._client.create_bookmark(body), done, failed)
