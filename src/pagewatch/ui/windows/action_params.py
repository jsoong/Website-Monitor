"""The "Configure..." dialog for one action of a bookmark (email, ntfy, webhook, export,
run_program, scrape, script).

Each form builds the same ``params`` dict the engine stores in ``actions_json``, and Save
validates it with the shared model (``pagewatch.models.ACTION_PARAMS``), so what the dialog
accepts is exactly what the engine will accept. Keys that equal the default are left out, which
keeps the stored JSON small and round-trips an untouched action unchanged.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import ACTION_PARAMS, ActionType

CONFIGURABLE = tuple(t.value for t in ACTION_PARAMS)
TITLES = {
    "email": "Email",
    "ntfy": "ntfy notification",
    "webhook": "Webhook",
    "export": "Export to a file",
    "run_program": "Run a program",
    "scrape": "Scrape fields to a file",
    "script": "Script",
}
PATH_HELP = (
    "Variables: {id} {name} {datetime} {change_id} {info1} {info2} {info3} and "
    "{date:<format>}, for example {date:%Y-%m-%d}. A relative path is inside the data folder's "
    "exports folder."
)


class SettingError(ValueError):
    """A free-form field is unusable (a number that is not a number, ...)."""


def _prune(params: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k not in defaults or v != defaults[k]}


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]


def _lines(widget: QPlainTextEdit) -> list[str]:
    return [ln.strip() for ln in widget.toPlainText().splitlines() if ln.strip()]


def _combo(items: list[tuple[str, str]], current: str) -> QComboBox:
    box = QComboBox()
    for label, value in items:
        box.addItem(label, value)
    box.setCurrentIndex(max(0, box.findData(current)))
    return box


class RowTable(QWidget):
    """A small editable table: text cells and drop-down cells, with Add and Remove buttons."""

    def __init__(self, columns: list[tuple[str, list[str] | None]], parent: Any = None) -> None:
        super().__init__(parent)
        self._columns = columns
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels([c[0] for c in columns])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        add = QPushButton("Add")
        remove = QPushButton("Remove")
        add.clicked.connect(lambda: self.add_row())
        remove.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def add_row(self, values: list[str] | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, (_, choices) in enumerate(self._columns):
            value = values[col] if values and col < len(values) else ""
            if choices is not None:
                box = QComboBox()
                box.addItems(choices)
                box.setCurrentIndex(max(0, box.findText(value)))
                self.table.setCellWidget(row, col, box)
            else:
                self.table.setItem(row, col, QTableWidgetItem(value))

    def _remove(self) -> None:
        for index in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(index)

    def rows(self) -> list[list[str]]:
        out: list[list[str]] = []
        for r in range(self.table.rowCount()):
            row: list[str] = []
            for c, (_, choices) in enumerate(self._columns):
                if choices is not None:
                    widget = self.table.cellWidget(r, c)
                    row.append(widget.currentText() if isinstance(widget, QComboBox) else "")
                else:
                    item = self.table.item(r, c)
                    row.append(item.text().strip() if item else "")
            out.append(row)
        return out


class ActionParamsDialog(QDialog):
    def __init__(self, action_type: str, params: dict[str, Any], parent: Any = None) -> None:
        super().__init__(parent)
        if action_type not in CONFIGURABLE:
            raise ValueError(f"{action_type} has no settings")
        self.action_type = action_type
        self._initial = dict(params)
        self.setWindowTitle(f"{TITLES[action_type]}: settings")
        self.resize(560, 420)
        self.form = QFormLayout()
        self._collect = getattr(self, f"_build_{action_type}")(dict(params))
        self.error = QLabel("")
        self.error.setStyleSheet("color: #b00020")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self._params: dict[str, Any] = dict(params)

    # -- result -------------------------------------------------------------------------

    def params(self) -> dict[str, Any]:
        return self._params

    def accept(self) -> None:
        try:
            collected = self._collect()
            ACTION_PARAMS[ActionType(self.action_type)].model_validate(collected)
        except SettingError as exc:
            self.error.setText(str(exc))
            return
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            msg = str(first["msg"]).removeprefix("Value error, ")
            self.error.setText(f"{where}: {msg}" if where else msg)
            return
        self._params = collected
        super().accept()

    # -- forms --------------------------------------------------------------------------

    def _note(self, text: str) -> None:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: gray")
        self.form.addRow(label)

    def _build_email(self, p: dict[str, Any]) -> Any:
        to = QLineEdit(", ".join(p.get("to", [])))
        to.setPlaceholderText("blank: the default recipients in Notifications settings")
        fmt = _combo(
            [
                ("Changes only", "changes_only"),
                ("Simple: summary and link", "simple"),
                ("Full page with the changes marked", "full"),
            ],
            p.get("format", "changes_only"),
        )
        shot = QCheckBox("Attach the screenshot (screenshot bookmarks)")
        shot.setChecked(bool(p.get("attach_screenshot", False)))
        priority = _combo(
            [("Normal", "normal"), ("High", "high"), ("Low", "low")], p.get("priority", "normal")
        )
        subject = QLineEdit(p.get("subject") or "")
        subject.setPlaceholderText("[PageWatch] {{ bookmark.name }} changed")
        template = QPlainTextEdit(p.get("template") or "")
        template.setPlaceholderText(
            "Optional plain-text body (Jinja2), replacing the built-in message:\n"
            "{{ bookmark.name }} changed:\n{{ change.added_text }}"
        )
        self.form.addRow("To", to)
        self.form.addRow("Format", fmt)
        self.form.addRow(shot)
        self.form.addRow("Priority", priority)
        self.form.addRow("Subject", subject)
        self.form.addRow("Own template", template)
        self._note("A private bookmark's email says only which bookmark changed.")

        def collect() -> dict[str, Any]:
            out = {
                "to": _csv(to.text()),
                "format": fmt.currentData(),
                "attach_screenshot": shot.isChecked(),
                "priority": priority.currentData(),
                "subject": subject.text().strip() or None,
                "template": template.toPlainText().strip() or None,
            }
            out = {k: v for k, v in out.items() if v is not None}
            return _prune(
                out,
                {"to": [], "format": "changes_only", "attach_screenshot": False,
                 "priority": "normal"},
            )  # fmt: skip

        return collect

    def _build_ntfy(self, p: dict[str, Any]) -> Any:
        server = QLineEdit(p.get("server") or "")
        server.setPlaceholderText("blank: the server in Notifications settings")
        topic = QLineEdit(p.get("topic") or "")
        topic.setPlaceholderText("blank: the topic in Notifications settings")
        priority = _combo(
            [("Default", "default"), ("Min", "min"), ("Low", "low"), ("High", "high"),
             ("Max", "max")],
            p.get("priority", "default"),
        )  # fmt: skip
        tags = QLineEdit(", ".join(p.get("tags", [])))
        tags.setPlaceholderText("comma separated, e.g. warning, tada")
        token = QLineEdit(p.get("token_key") or "")
        token.setPlaceholderText("blank: the token in Notifications settings")
        self.form.addRow("Server", server)
        self.form.addRow("Topic", topic)
        self.form.addRow("Priority", priority)
        self.form.addRow("Tags", tags)
        self.form.addRow("Token secret name", token)
        self._note(
            "On the public ntfy.sh server the topic name is the only protection. Use a "
            "self-hosted server or an access-protected topic for anything private."
        )

        def collect() -> dict[str, Any]:
            out: dict[str, Any] = {
                "server": server.text().strip() or None,
                "topic": topic.text().strip() or None,
                "priority": priority.currentData(),
                "tags": _csv(tags.text()),
                "token_key": token.text().strip() or None,
            }
            out = {k: v for k, v in out.items() if v is not None}
            return _prune(out, {"priority": "default", "tags": []})

        return collect

    def _build_webhook(self, p: dict[str, Any]) -> Any:
        url = QLineEdit(p.get("url") or "")
        url.setPlaceholderText("https://example.com/hook")
        method = _combo([(m, m) for m in ("POST", "PUT", "PATCH", "GET")], p.get("method", "POST"))
        body = QPlainTextEdit(p.get("body") or "")
        body.setPlaceholderText(
            'Optional JSON body (Jinja2), for example:\n{"text": {{ change.summary|tojson }}}\n'
            "Blank: a standard JSON object."
        )
        headers = QPlainTextEdit(
            "\n".join(f"{k}: {v}" for k, v in (p.get("headers") or {}).items())
        )
        headers.setPlaceholderText("Name: value, one per line")
        headers.setMaximumHeight(70)
        token = QLineEdit(p.get("token_key") or "")
        token.setPlaceholderText("secret name of a bearer token (optional)")
        self.form.addRow("URL", url)
        self.form.addRow("Method", method)
        self.form.addRow("Body", body)
        self.form.addRow("Headers", headers)
        self.form.addRow("Token secret name", token)

        def collect() -> dict[str, Any]:
            hdrs: dict[str, str] = {}
            for line in _lines(headers):
                name, sep, value = line.partition(":")
                if not sep:
                    raise SettingError(f"Header line {line!r} must be 'Name: value'")
                hdrs[name.strip()] = value.strip()
            out: dict[str, Any] = {
                "url": url.text().strip(),
                "method": method.currentData(),
                "body": body.toPlainText().strip() or None,
                "headers": hdrs,
                "token_key": token.text().strip() or None,
            }
            out = {k: v for k, v in out.items() if v is not None}
            return _prune(out, {"method": "POST", "headers": {}})

        return collect

    def _build_export(self, p: dict[str, Any]) -> Any:
        path = QLineEdit(p.get("path") or "")
        path.setPlaceholderText("C:\\Users\\me\\Documents\\PageWatch\\{name}\\{date:%Y-%m-%d}.html")
        fmt = _combo(
            [
                ("HTML (the page, changes marked)", "html"),
                ("HTML with images and styles saved beside it", "html_assets"),
                ("Text (the changes)", "text"),
                ("JSON (the diff)", "diff_json"),
            ],
            p.get("format", "html"),
        )
        highlight = QCheckBox("Mark the changes in the HTML")
        highlight.setChecked(bool(p.get("highlight", True)))
        self.form.addRow("File", path)
        self.form.addRow("Format", fmt)
        self.form.addRow(highlight)
        self._note(PATH_HELP)

        def collect() -> dict[str, Any]:
            out = {
                "path": path.text().strip(),
                "format": fmt.currentData(),
                "highlight": highlight.isChecked(),
            }
            return _prune(out, {"format": "html", "highlight": True})

        return collect

    def _build_run_program(self, p: dict[str, Any]) -> Any:
        exe = QLineEdit(p.get("executable") or "")
        exe.setPlaceholderText("C:\\Tools\\notify.exe")
        args = QPlainTextEdit("\n".join(p.get("args", [])))
        args.setPlaceholderText("One argument per line, e.g.\n--url\n{url}\n{file_changes}")
        timeout = QDoubleSpinBox()
        timeout.setRange(1, 3600)
        timeout.setValue(float(p.get("timeout_s", 60.0)))
        cwd = QLineEdit(p.get("cwd") or "")
        confirm = QCheckBox(
            "I understand this starts this program on this PC every time the bookmark changes"
        )
        confirm.setChecked(bool(p.get("confirmed", False)))
        self.form.addRow("Program", exe)
        self.form.addRow("Arguments", args)
        self.form.addRow("Timeout (s)", timeout)
        self.form.addRow("Working folder", cwd)
        self.form.addRow(confirm)
        self._note(
            "No shell is used: each line is exactly one argument. Variables: {url} {file_new} "
            "{file_old} {file_changes} {info1} {info2} {info3} {id} {name} {change_id} "
            "{datetime}."
        )

        def collect() -> dict[str, Any]:
            out: dict[str, Any] = {
                "executable": exe.text().strip(),
                "args": [ln for ln in args.toPlainText().splitlines() if ln.strip()],
                "timeout_s": timeout.value(),
                "cwd": cwd.text().strip() or None,
                "confirmed": confirm.isChecked(),
            }
            out = {k: v for k, v in out.items() if v is not None}
            return _prune(out, {"args": [], "timeout_s": 60.0})

        return collect

    def _build_scrape(self, p: dict[str, Any]) -> Any:
        path = QLineEdit(p.get("path") or "")
        path.setPlaceholderText("C:\\Users\\me\\Documents\\PageWatch\\prices.csv")
        fmt = _combo(
            [("CSV", "csv"), ("JSON Lines", "jsonl"), ("XML", "xml")], p.get("format", "csv")
        )
        table = RowTable(
            [
                ("Name", None),
                ("Kind", ["css", "xpath", "regex"]),
                ("Expression", None),
                ("Attribute", None),
            ]
        )
        for f in p.get("fields", []):
            table.add_row([f.get("name", ""), f.get("kind", "css"), f.get("expr", ""),
                           f.get("attr") or ""])  # fmt: skip
        multiple = QCheckBox("Join all matches with ' | ' (otherwise the first)")
        multiple.setChecked(any(f.get("multiple") for f in p.get("fields", [])))
        self.form.addRow("File", path)
        self.form.addRow("Format", fmt)
        self.form.addRow("Fields", table)
        self.form.addRow(multiple)
        self._note(
            "One record is appended per change, with the change id, time, bookmark and URL "
            "first. CSS and XPath read the element's text (or the attribute); a regex runs over "
            "the page text and returns group 1 if it has one. " + PATH_HELP
        )

        def collect() -> dict[str, Any]:
            fields: list[dict[str, Any]] = []
            for name, kind, expr, attr in table.rows():
                if not (name or expr):
                    continue
                f: dict[str, Any] = {"name": name, "kind": kind, "expr": expr}
                if attr:
                    f["attr"] = attr
                if multiple.isChecked():
                    f["multiple"] = True
                fields.append(f)
            out = {"path": path.text().strip(), "format": fmt.currentData(), "fields": fields}
            return _prune(out, {"format": "csv"})

        return collect

    def _build_script(self, p: dict[str, Any]) -> Any:
        table = RowTable([("Step", ["check_folder", "pause_bookmark"]), ("Folder / minutes", None)])
        for s in p.get("steps", []):
            table.add_row(
                [s.get("op", "check_folder"), str(s.get("name") or s.get("minutes") or "")]
            )
        self.form.addRow("Steps", table)
        self._note(
            "check_folder: check every bookmark in that folder now. pause_bookmark: do not check "
            "this bookmark again for that many minutes."
        )

        def collect() -> dict[str, Any]:
            steps: list[dict[str, Any]] = []
            for op, value in table.rows():
                if op == "check_folder":
                    steps.append({"op": op, "name": value})
                else:
                    try:
                        steps.append({"op": op, "minutes": int(value)})
                    except ValueError as exc:
                        raise SettingError(
                            f"pause_bookmark needs a whole number of minutes, not {value!r}"
                        ) from exc
            return {"steps": steps}

        return collect
