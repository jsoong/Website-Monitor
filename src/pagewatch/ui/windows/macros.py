"""Check-Macros: the list, an editor for the steps, and "Record macro" (spec: Desktop UI ->
Other screens: *the Macro recorder*; Fetch layer -> Logins and Check-Macros).

A macro is a short list of steps (goto, fill, click, select, press, wait_for, assert_text) run
before a check, when the site shows its login page. The steps are edited as JSON, the format the
engine stores, and are validated here with the same models the engine uses, so an error is shown
before anything is saved. Passwords are never typed into a step: a password field takes
``{"secret": "<key>"}`` and the password goes to the credential store under that key (the table
under the steps does that).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from pagewatch.models import MacroDef, MacroOut, RecordOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

POLL_MS = 1000
TEMPLATE = [
    {"op": "goto", "url": "https://example.com/login"},
    {"op": "fill", "selector": "#username", "value": "your user name"},
    {"op": "fill", "selector": "#password", "value": {"secret": "example-password"}},
    {"op": "click", "selector": "button[type=submit]"},
    {"op": "assert_text", "text": "Sign out"},
]


def steps_text(steps: list[Any]) -> str:
    """Steps as the JSON the editor shows (defaults left out, so it stays short)."""
    plain = [s.model_dump(mode="json", exclude_defaults=True) for s in steps]
    return json.dumps(plain, indent=2)


def problem_text(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = " -> ".join(str(p) for p in first["loc"] if p != "steps")
    msg = str(first["msg"]).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


class RecordDialog(QDialog):
    """Runs Playwright codegen through the engine and waits for the recording to finish."""

    def __init__(self, client: ApiClient, parent: Any = None, *, url: str = "") -> None:
        super().__init__(parent)
        self._client = client
        self._id: str | None = None
        self.result_steps: list[Any] = []
        self.setWindowTitle("Record macro")
        self.resize(560, 420)
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("https://example.com/login")
        self.password_key = QLineEdit("macro-password")
        self.password_key.setToolTip(
            "A password typed while recording is not kept; the step points at this name and you "
            "store the password under it afterwards"
        )
        self.status = QLabel(
            "Choose the page to start on, then press Start. A browser window opens;"
        )
        self.status.setWordWrap(True)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("The recorded steps appear here for review.")
        self.warnings = QLabel("")
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet("color: #b26a00")
        self.btn_start = QPushButton("Start recording")
        self.btn_use = QPushButton("Use these steps")
        self.btn_use.setEnabled(False)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_start.clicked.connect(self.start)
        self.btn_use.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.cancel)
        form = QFormLayout()
        form.addRow("Start page", self.url)
        form.addRow("Password is stored as", self.password_key)
        row = QHBoxLayout()
        row.addWidget(self.btn_start)
        row.addStretch(1)
        row.addWidget(self.btn_use)
        row.addWidget(self.btn_cancel)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.warnings)
        layout.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        url, key = self.url.text().strip(), self.password_key.text().strip()
        self.btn_start.setEnabled(False)
        self.status.setText("Opening the recorder...")

        def done(rec: RecordOut) -> None:
            self._id = rec.id
            self._show(rec)
            if rec.state == "running":
                self._timer.start()

        def failed(exc: Exception) -> None:
            self.btn_start.setEnabled(True)
            self.status.setText(str(exc).split(": ", 1)[-1])

        run_async(lambda: self._client.start_recording(url, key), done, failed)

    def _poll(self) -> None:
        rec_id = self._id
        if rec_id is None:
            return
        run_async(lambda: self._client.recording(rec_id), self._show, lambda e: self._lost(e))

    def _lost(self, exc: Exception) -> None:
        self._timer.stop()
        self.status.setText(f"Lost touch with the recorder: {exc}")
        self.btn_start.setEnabled(True)

    def _show(self, rec: RecordOut) -> None:
        if rec.state == "running":
            self.status.setText(
                "Recording. Log in on the page that opened, then close the recorder window."
            )
            return
        self._timer.stop()
        self.btn_start.setEnabled(True)
        self.warnings.setText("\n".join(rec.warnings))
        if rec.state == "done":
            self.result_steps = list(rec.steps)
            self.preview.setPlainText(steps_text(self.result_steps))
            self.status.setText(f"Recorded {len(rec.steps)} step(s). Review them, then use them.")
            self.btn_use.setEnabled(True)
        else:
            self.status.setText(rec.error or "The recording did not finish.")

    def cancel(self) -> None:
        self._timer.stop()
        rec_id = self._id
        if rec_id is not None and self.btn_start.isEnabled() is False:
            run_async(lambda: self._client.cancel_recording(rec_id), None, None)
        self.reject()


class MacrosDialog(QDialog):
    """List, edit, record and delete macros."""

    macros_changed = Signal()

    def __init__(self, client: ApiClient, parent: Any = None) -> None:
        super().__init__(parent)
        self._client = client
        self._macros: list[MacroOut] = []
        self._current: int | None = None  # id of the macro being edited, None = a new one
        self.setWindowTitle("Macros")
        self.resize(860, 560)

        self.listing = QListWidget()
        self.listing.setObjectName("macroList")
        self.listing.setMaximumWidth(220)
        self.name = QLineEdit()
        self.steps = QPlainTextEdit()
        self.steps.setObjectName("macroSteps")
        self.steps.setPlaceholderText("Steps as JSON. Press New for an example.")
        self.sig_selector = QLineEdit()
        self.sig_selector.setPlaceholderText("input[type=password]")
        self.sig_url = QLineEdit()
        self.sig_url.setPlaceholderText("/login")
        self.sig_text = QLineEdit()
        self.sig_text.setPlaceholderText("Please sign in")
        self.secrets = QTableWidget(0, 3)
        self.secrets.setObjectName("macroSecrets")
        self.secrets.setHorizontalHeaderLabels(["Password name", "Stored?", ""])
        self.secrets.verticalHeader().hide()
        self.secrets.setMaximumHeight(120)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #c62828")
        self.error.setWordWrap(True)

        self.btn_new = QPushButton("New")
        self.btn_record = QPushButton("Record...")
        self.btn_save = QPushButton("Save")
        self.btn_delete = QPushButton("Delete")
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.btn_new.clicked.connect(self.new_macro)
        self.btn_record.clicked.connect(self.record)
        self.btn_save.clicked.connect(self.save)
        self.btn_delete.clicked.connect(self.delete)
        close.rejected.connect(self.accept)
        self.listing.currentRowChanged.connect(self._selected)
        self.steps.textChanged.connect(self._steps_edited)
        self._secret_timer = QTimer(self)
        self._secret_timer.setSingleShot(True)
        self._secret_timer.setInterval(300)
        self._secret_timer.timeout.connect(self.refresh_secrets)

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow(QLabel("Steps"))
        sig = QFormLayout()
        sig.addRow("Login page has", self.sig_selector)
        sig.addRow("or the address has", self.sig_url)
        sig.addRow("or the page says", self.sig_text)
        hint = QLabel(
            "The macro runs only when the check lands on a login page. Say how it is recognised:"
        )
        hint.setWordWrap(True)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(self.steps, 1)
        right.addWidget(QLabel("Passwords used by these steps"))
        right.addWidget(self.secrets)
        right.addWidget(hint)
        right.addLayout(sig)
        right.addWidget(self.error)
        buttons = QHBoxLayout()
        for b in (self.btn_new, self.btn_record, self.btn_save, self.btn_delete):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        right.addLayout(buttons)
        left = QVBoxLayout()
        left.addWidget(QLabel("Macros"))
        left.addWidget(self.listing, 1)
        layout = QHBoxLayout(self)
        layout.addLayout(left)
        layout.addLayout(right, 1)
        self.reload()

    # -- data ---------------------------------------------------------------------------

    def reload(self, select: int | None = None) -> None:
        def done(items: list[MacroOut]) -> None:
            self._macros = items
            self.listing.blockSignals(True)
            self.listing.clear()
            for m in items:
                item = QListWidgetItem(m.name)
                item.setData(Qt.ItemDataRole.UserRole, m.id)
                item.setToolTip(f"{len(m.steps)} step(s), used by {m.bookmarks} bookmark(s)")
                self.listing.addItem(item)
            self.listing.blockSignals(False)
            want = select if select is not None else self._current
            for r, m in enumerate(items):
                if m.id == want:
                    self.listing.setCurrentRow(r)
                    return
            if want is None and items and self.steps.toPlainText() == "":
                self.listing.setCurrentRow(0)

        run_async(self._client.macros, done, lambda e: self.error.setText(str(e)))

    def _selected(self, row: int) -> None:
        if not 0 <= row < len(self._macros):
            return
        m = self._macros[row]
        self._current = m.id
        self.name.setText(m.name)
        self.steps.setPlainText(steps_text(list(m.steps)))
        sig = m.login_signal
        self.sig_selector.setText(sig.selector or "" if sig else "")
        self.sig_url.setText(sig.url_contains or "" if sig else "")
        self.sig_text.setText(sig.text or "" if sig else "")
        self.error.setText("")
        self.refresh_secrets()

    def new_macro(self) -> None:
        self._current = None
        self.listing.blockSignals(True)
        self.listing.clearSelection()
        self.listing.setCurrentRow(-1)
        self.listing.blockSignals(False)
        self.name.setText("")
        self.steps.setPlainText(json.dumps(TEMPLATE, indent=2))
        for e in (self.sig_selector, self.sig_url, self.sig_text):
            e.setText("")
        self.sig_selector.setText("input[type=password]")
        self.error.setText("")
        self.refresh_secrets()

    # -- the form -----------------------------------------------------------------------

    def body(self) -> dict[str, Any]:
        """The macro as the engine takes it. Raises ``ValueError`` with a sentence."""
        try:
            steps = json.loads(self.steps.toPlainText() or "[]")
        except ValueError as exc:
            raise ValueError(f"The steps are not valid JSON: {exc}") from exc
        if not isinstance(steps, list):
            raise ValueError("The steps are a JSON list: [ {...}, {...} ]")
        signal = {
            "selector": self.sig_selector.text().strip() or None,
            "url_contains": self.sig_url.text().strip() or None,
            "text": self.sig_text.text().strip() or None,
        }
        raw = {
            "name": self.name.text().strip(),
            "steps": steps,
            "login_signal": signal if any(signal.values()) else None,
        }
        try:
            return MacroDef.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError(problem_text(exc)) from exc

    def save(self) -> None:
        try:
            body = self.body()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.error.setText("")
        current = self._current

        def work() -> MacroOut:
            if current is None:
                return self._client.create_macro(body)
            return self._client.replace_macro(current, body)

        def done(m: MacroOut) -> None:
            self._current = m.id
            self.macros_changed.emit()
            self.reload(select=m.id)

        run_async(work, done, lambda e: self.error.setText(str(e).split(": ", 1)[-1]))

    def delete(self) -> None:
        current = self._current
        if current is None:
            return
        used = next((m.bookmarks for m in self._macros if m.id == current), 0)
        if not self._confirm(
            "Delete macro",
            "Delete this macro?"
            + (f"\n{used} bookmark(s) use it and will stop running it." if used else ""),
        ):
            return

        def done(_: Any) -> None:
            self._current = None
            self.macros_changed.emit()
            self.name.setText("")
            self.steps.setPlainText("")
            self.reload()

        run_async(
            lambda: self._client.delete_macro(current), done, lambda e: self.error.setText(str(e))
        )

    def _confirm(self, title: str, text: str) -> bool:  # a seam: tests do not open a message box
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    # -- recording ----------------------------------------------------------------------

    def record(self) -> None:
        dialog = self._make_recorder()
        if self._exec(dialog) and dialog.result_steps:
            self.steps.setPlainText(steps_text(dialog.result_steps))
            self.error.setText(
                "Recorded. Review the steps, give the macro a name and a login page, then Save."
                + ("\n" + dialog.warnings.text() if dialog.warnings.text() else "")
            )

    def _make_recorder(self) -> RecordDialog:
        return RecordDialog(self._client, self)

    def _exec(self, dialog: QDialog) -> bool:  # a seam for tests
        return bool(dialog.exec())

    # -- passwords ----------------------------------------------------------------------

    def secret_keys(self) -> list[str]:
        try:
            steps = json.loads(self.steps.toPlainText() or "[]")
        except ValueError:
            return []
        keys: set[str] = set()
        if isinstance(steps, list):
            for s in steps:
                value = s.get("value") if isinstance(s, dict) else None
                if isinstance(value, dict) and isinstance(value.get("secret"), str):
                    keys.add(value["secret"])
        return sorted(keys)

    def _steps_edited(self) -> None:
        self._secret_timer.start()

    def refresh_secrets(self) -> None:
        keys = self.secret_keys()
        self.secrets.setRowCount(len(keys))
        for r, key in enumerate(keys):
            self.secrets.setItem(r, 0, QTableWidgetItem(key))
            self.secrets.setItem(r, 1, QTableWidgetItem("checking..."))
            button = QPushButton("Set password...")
            button.clicked.connect(lambda _c=False, k=key: self.set_password(k))
            self.secrets.setCellWidget(r, 2, button)

            def done(out: Any, r: int = r, key: str = key) -> None:
                name, item = self.secrets.item(r, 0), self.secrets.item(r, 1)
                if name is not None and item is not None and name.text() == key:
                    item.setText("yes" if out.exists else "no" if out.available else "no store")

            run_async(lambda k=key: self._client.secret(k), done, None)

    def set_password(self, key: str) -> None:
        password = self._ask_password(key)
        if not password:
            return

        def done(_: Any) -> None:
            self.error.setText(f"Password stored as {key!r}.")
            self.refresh_secrets()

        run_async(
            lambda: self._client.put_secret(key, password),
            done,
            lambda e: self.error.setText(str(e).split(": ", 1)[-1]),
        )

    def _ask_password(self, key: str) -> str:  # a seam: tests do not open a prompt
        text, ok = QInputDialog.getText(
            self, "Set password", f"Password for {key!r} (kept in the credential store):",
            QLineEdit.EchoMode.Password,
        )  # fmt: skip
        return text if ok else ""
