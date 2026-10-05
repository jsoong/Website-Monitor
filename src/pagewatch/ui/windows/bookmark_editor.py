"""The bookmark editor: General, Schedule, Filters (with live Test filter), Keywords, Gate,
Highlight, Actions, Login, Advanced and Notes. It edits *effective* values and sends only what
changed as a ``PATCH`` (so untouched settings stay inherited); in bulk mode each tab has an
"apply to all" box and sends that tab's full values to every selected bookmark."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from pagewatch.models import (
    DAYS,
    ActionType,
    BookmarkOut,
    FilterRule,
    FolderOut,
    TestFilterOut,
)
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

IMPLEMENTED_ACTIONS = {"toast", "sound", "open", "mark_read"}
UNITS = [("minutes", 60), ("hours", 3600), ("days", 86400)]
RULE_LISTS = ("cosmetic", "watch", "ignore")
RULE_TYPES = ("selector", "between", "text", "number_mask")
RULE_KINDS = ("", "css", "xpath", "literal", "wildcard", "regex")
SPECIALS = [
    ("text_only", "Compare text only (ignore HTML tags)"),
    ("ignore_case", "Ignore case"),
    ("normalize_unicode", "Normalize Unicode and whitespace"),
    ("ignore_options", "Ignore dropdown and list-box entries"),
    ("sort_content", "Sort content (a reorder is not a change)"),
    ("watch_links", "Watch link URLs"),
    ("watch_images", "Watch image URLs"),
]


def diff_dict(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """The keys of ``new`` that differ from ``old`` (nested dicts recursively; ``None`` in the
    result means "remove the override")."""
    out: dict[str, Any] = {}
    for key, value in new.items():
        before = old.get(key)
        if before == value:
            continue
        if isinstance(value, dict) and isinstance(before, dict):
            sub = diff_dict(before, value)
            if sub:
                out[key] = sub
        else:
            out[key] = value
    return out


def best_unit(seconds: int) -> tuple[int, int]:
    for _, size in reversed(UNITS):
        if seconds % size == 0:
            return seconds // size, size
    return max(1, seconds // 60), 60


def _lines(widget: QPlainTextEdit) -> list[str]:
    return [ln.strip() for ln in widget.toPlainText().splitlines() if ln.strip()]


class BookmarkEditor(QDialog):
    saved = Signal()
    save_failed = Signal(str)

    def __init__(
        self,
        client: ApiClient,
        bookmarks: list[BookmarkOut],
        folders: list[FolderOut],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._all = bookmarks
        self._orig = bookmarks[0]
        self._folders = folders
        self.bulk = len(bookmarks) > 1
        self.setWindowTitle(
            f"Edit {len(bookmarks)} bookmarks" if self.bulk else f"Edit bookmark: {self._orig.name}"
        )
        self.resize(720, 560)
        self._apply: dict[str, QCheckBox] = {}
        self.tabs = QTabWidget()
        self.error = QLabel("")
        self.error.setStyleSheet("color: #c62828")
        self.error.setWordWrap(True)
        self.test_result = QPlainTextEdit()
        self.test_result.setReadOnly(True)
        self.test_summary = QLabel("")

        self._build_general()
        self._build_schedule()
        self._build_filters()
        self._build_keywords()
        self._build_gate()
        self._build_highlight()
        self._build_actions()
        self._build_login()
        self._build_advanced()
        self._build_notes()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    # -- tab scaffolding ----------------------------------------------------------------

    def _tab(self, title: str) -> QFormLayout:
        page = QWidget()
        form = QFormLayout(page)
        if self.bulk:
            box = QCheckBox(f"Apply this tab to all {len(self._all)} bookmarks")
            self._apply[title] = box
            form.addRow(box)
        self.tabs.addTab(page, title)
        return form

    # -- General ------------------------------------------------------------------------

    def _build_general(self) -> None:
        o = self._orig
        form = self._tab("General")
        self.name = QLineEdit(o.name)
        self.url = QLineEdit(o.url)
        self.folder = QComboBox()
        self.folder.addItem("(no folder)", None)
        for f in sorted(self._folders, key=lambda f: f.name.lower()):
            if not f.is_virtual:
                self.folder.addItem(f.name, f.id)
        self.folder.setCurrentIndex(max(0, self.folder.findData(o.folder_id)))
        self.method = QComboBox()
        for m in ("auto", "static", "browser", "screenshot"):
            self.method.addItem(m, m)
        self.method.setCurrentIndex(self.method.findData(o.check_method.value))
        self.source = QComboBox()
        for s in (
            "auto",
            "html",
            "feed",
            "pdf",
            "docx",
            "xlsx",
            "ftp",
            "file",
            "folder",
            "image",
            "binary",
            "records",
        ):
            self.source.addItem(s, s)
        self.source.setCurrentIndex(self.source.findData(o.source_type.value))
        self.enabled = QCheckBox("Check this bookmark")
        self.enabled.setChecked(o.enabled)
        self.hotsite = QCheckBox("Hotsite: jump the queue")
        self.hotsite.setChecked(bool(o.priority))
        if not self.bulk:
            form.addRow("Name", self.name)
            form.addRow("URL", self.url)
        form.addRow("Folder", self.folder)
        form.addRow("Check method", self.method)
        form.addRow("Source type", self.source)
        form.addRow(self.enabled)
        form.addRow(self.hotsite)

    # -- Schedule -----------------------------------------------------------------------

    def _build_schedule(self) -> None:
        s = self._orig.schedule
        form = self._tab("Schedule")
        self.mode = QComboBox()
        for m in ("interval", "times", "adaptive", "manual"):
            self.mode.addItem(m, m)
        self.mode.setCurrentIndex(self.mode.findData(s.mode.value))
        value, size = best_unit(s.interval_s)
        self.interval = QSpinBox()
        self.interval.setRange(1, 100000)
        self.interval.setValue(value)
        self.unit = QComboBox()
        for label, secs in UNITS:
            self.unit.addItem(label, secs)
        self.unit.setCurrentIndex(self.unit.findData(size))
        self.times = QLineEdit(", ".join(s.times))
        self.times.setPlaceholderText("09:00, 17:30")
        self.days = {d: QCheckBox(d.capitalize()) for d in DAYS}
        days_row = QHBoxLayout()
        for d, box in self.days.items():
            box.setChecked(not s.days or d in s.days)
            days_row.addWidget(box)
        self.window_on = QCheckBox("Only between")
        self.window_on.setChecked(s.window is not None)
        self.win_start, self.win_end = QTimeEdit(), QTimeEdit()
        for edit, text in ((self.win_start, s.window.start if s.window else "07:00"),
                           (self.win_end, s.window.end if s.window else "23:00")):  # fmt: skip
            edit.setDisplayFormat("HH:mm")
            h, m = text.split(":")
            edit.setTime(edit.time().fromString(f"{h}:{m}", "HH:mm"))
        win_row = QHBoxLayout()
        for w in (self.window_on, self.win_start, QLabel("and"), self.win_end):
            win_row.addWidget(w)
        self.ad_min, self.ad_max = QSpinBox(), QSpinBox()
        for spin, secs in ((self.ad_min, s.adaptive.min_s), (self.ad_max, s.adaptive.max_s)):
            spin.setRange(1, 100000)
            spin.setValue(max(1, secs // 60))
            spin.setSuffix(" min")
        self.ad_factor = QDoubleSpinBox()
        self.ad_factor.setRange(1.01, 10.0)
        self.ad_factor.setSingleStep(0.1)
        self.ad_factor.setValue(s.adaptive.factor)
        self.jitter = QSpinBox()
        self.jitter.setRange(0, 50)
        self.jitter.setSuffix(" %")
        self.jitter.setValue(s.jitter_pct)
        self.battery = QComboBox()
        for b in ("normal", "slow", "pause"):
            self.battery.addItem(b, b)
        self.battery.setCurrentIndex(self.battery.findData(s.on_battery.value))
        interval_row = QHBoxLayout()
        interval_row.addWidget(self.interval)
        interval_row.addWidget(self.unit)
        form.addRow("Mode", self.mode)
        form.addRow("Every", interval_row)
        form.addRow("At (times mode)", self.times)
        form.addRow("Days", days_row)
        form.addRow("Window", win_row)
        form.addRow("Adaptive: shortest", self.ad_min)
        form.addRow("Adaptive: longest", self.ad_max)
        form.addRow("Adaptive: factor", self.ad_factor)
        form.addRow("Jitter", self.jitter)
        form.addRow("On battery", self.battery)

    def schedule_dict(self) -> dict[str, Any]:
        days = [d for d, box in self.days.items() if box.isChecked()]
        window = (
            {
                "start": self.win_start.time().toString("HH:mm"),
                "end": self.win_end.time().toString("HH:mm"),
            }
            if self.window_on.isChecked()
            else None
        )
        return {
            "mode": self.mode.currentData(),
            "interval_s": max(60, self.interval.value() * int(self.unit.currentData())),
            "times": sorted({t.strip() for t in self.times.text().split(",") if t.strip()}),
            "days": [] if len(days) == 7 else days,
            "window": window,
            "adaptive": {
                "min_s": self.ad_min.value() * 60,
                "max_s": max(self.ad_max.value(), self.ad_min.value()) * 60,
                "factor": round(self.ad_factor.value(), 2),
            },
            "jitter_pct": self.jitter.value(),
            "on_battery": self.battery.currentData(),
        }

    # -- Filters ------------------------------------------------------------------------

    def _build_filters(self) -> None:
        f = self._orig.filter
        form = self._tab("Filters")
        self.rules = QTableWidget(0, 6)
        self.rules.setHorizontalHeaderLabels(
            ["List", "Type", "Kind", "Value / start", "End", "Scope"]
        )
        self.rules.horizontalHeader().setStretchLastSection(True)
        for list_name in RULE_LISTS:
            for rule in getattr(f, list_name):
                self.add_rule(list_name, rule)
        add, remove = QPushButton("Add rule"), QPushButton("Remove rule")
        add.clicked.connect(lambda: self.add_rule("ignore"))
        remove.clicked.connect(lambda: self.rules.removeRow(self.rules.currentRow()))
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        self.builtin = QCheckBox("Remove cookie banners (built-in list)")
        self.builtin.setChecked(f.builtin_cosmetic)
        self.specials = {}
        form.addRow(self.rules)
        form.addRow(row)
        form.addRow(self.builtin)
        for key, label in SPECIALS:
            box = QCheckBox(label)
            box.setChecked(getattr(f.special, key))
            self.specials[key] = box
            form.addRow(box)
        self.test_btn = QPushButton("Test filter against the stored pages")
        self.test_btn.setEnabled(not self.bulk)
        self.test_btn.clicked.connect(self.run_test_filter)
        form.addRow(self.test_btn)
        form.addRow(self.test_summary)
        form.addRow(self.test_result)

    def add_rule(self, list_name: str = "ignore", rule: FilterRule | None = None) -> None:
        r = self.rules.rowCount()
        self.rules.insertRow(r)
        for col, options in ((0, RULE_LISTS), (1, RULE_TYPES), (2, RULE_KINDS)):
            combo = QComboBox()
            combo.addItems(options)
            self.rules.setCellWidget(r, col, combo)
        for col in (3, 4, 5):
            self.rules.setCellWidget(r, col, QLineEdit())
        self._set(r, 0, list_name)
        if rule is not None:
            self._set(r, 1, rule.type)
            kind = (
                rule.selector_kind
                if rule.type == "selector"
                else (rule.pattern_kind if rule.type == "text" else "")
            )
            self._set(r, 2, kind)
            value = (rule.selector if rule.type == "selector" else rule.start if rule.type == "between"
                     else rule.pattern or "")  # fmt: skip
            self._set(r, 3, value or "")
            self._set(r, 4, rule.end or "")
            self._set(r, 5, rule.scope or "")

    def _set(self, row: int, col: int, value: str) -> None:
        w = self.rules.cellWidget(row, col)
        if isinstance(w, QComboBox):
            w.setCurrentText(value)
        elif isinstance(w, QLineEdit):
            w.setText(value)

    def _get(self, row: int, col: int) -> str:
        w = self.rules.cellWidget(row, col)
        return (
            w.currentText()
            if isinstance(w, QComboBox)
            else (w.text().strip() if isinstance(w, QLineEdit) else "")
        )

    def filter_dict(self) -> dict[str, Any]:
        """The filter configuration from the widgets (rules validated and normalised)."""
        lists: dict[str, list[dict[str, Any]]] = {name: [] for name in RULE_LISTS}
        for r in range(self.rules.rowCount()):
            kind, type_ = self._get(r, 2), self._get(r, 1)
            value, end, scope = self._get(r, 3), self._get(r, 4), self._get(r, 5)
            raw: dict[str, Any] = {"type": type_}
            if type_ == "selector":
                if not value:
                    continue
                raw |= {
                    "selector": value,
                    "selector_kind": kind if kind in ("css", "xpath") else "css",
                }
            elif type_ == "between":
                if not value and not end:
                    continue
                raw |= {"start": value or None, "end": end or None}
            elif type_ == "text":
                if not value:
                    continue
                raw |= {
                    "pattern": value,
                    "pattern_kind": kind if kind in ("literal", "wildcard", "regex") else "literal",
                }
            elif value:
                raw["pattern"] = value
            if scope:
                raw["scope"] = scope
            rule = FilterRule.model_validate(raw)
            lists[self._get(r, 0)].append(rule.model_dump(mode="json", exclude_defaults=True))
        return {
            **lists,
            "builtin_cosmetic": self.builtin.isChecked(),
            "special": {k: box.isChecked() for k, box in self.specials.items()},
        }

    def run_test_filter(self) -> None:
        try:
            candidate = {"filter": self.filter_dict()}
        except ValidationError as exc:
            self.test_summary.setText(f"Invalid rule: {exc.errors()[0]['msg']}")
            return
        self.test_summary.setText("Testing...")

        def done(out: TestFilterOut) -> None:
            verdict = "WOULD ALERT" if out.alert else f"would not alert ({out.reason})"
            self.test_summary.setText(
                f"{verdict}; {'identical pages' if out.identical else 'pages differ'}"
            )
            self.test_result.setPlainText("\n".join(out.marks) or "(no text)")

        run_async(
            lambda: self._client.test_filter(self._orig.id, candidate),
            done,
            lambda exc: self.test_summary.setText(str(exc).split(": ", 1)[-1]),
        )

    # -- Keywords / Gate / Highlight ----------------------------------------------------

    def _build_keywords(self) -> None:
        g = self._orig.gate
        form = self._tab("Keywords")
        form.addRow(QLabel(
            "One rule per line, lines are OR-ed. word, \"whole word\", a + b (AND), -term (NOT), "
            "page(term), regex(...), num(regex) < 1200, [same_block], [near N], #color."
        ))  # fmt: skip
        self.keywords = QPlainTextEdit(g.keywords)
        self.highlight_keywords = QPlainTextEdit(g.highlight_keywords)
        form.addRow("Alert when changes match", self.keywords)
        form.addRow("Highlight only (never gates)", self.highlight_keywords)

    def _build_gate(self) -> None:
        g = self._orig.gate
        form = self._tab("Gate")
        self.min_chars = QSpinBox()
        self.min_chars.setRange(0, 1_000_000)
        self.min_chars.setValue(g.min_chars)
        self.blacklist = QPlainTextEdit("\n".join(g.blacklist))
        self.whitelist = QPlainTextEdit("\n".join(g.whitelist))
        self.min_words = QSpinBox()
        self.min_words.setRange(0, 1_000_000)
        self.min_words.setValue(g.min_changed_words)
        self.threshold_mode = QComboBox()
        self.threshold_mode.addItem("Cumulative: changes since the last alert add up", "cumulative")
        self.threshold_mode.addItem("Per check: one check must change enough", "per_check")
        self.threshold_mode.setCurrentIndex(self.threshold_mode.findData(g.threshold_mode.value))
        self.ignore_removed = QCheckBox("Ignore removed content")
        self.ignore_removed.setChecked(g.ignore_removed)
        self.error_threshold = QSpinBox()
        self.error_threshold.setRange(1, 100)
        self.error_threshold.setValue(g.error_threshold)
        form.addRow("Ignore pages shorter than (chars; 100 suggested)", self.min_chars)
        form.addRow("Ignore pages containing (one per line)", self.blacklist)
        form.addRow("Only pages containing one of", self.whitelist)
        form.addRow("Minimum changed words", self.min_words)
        form.addRow("Threshold mode", self.threshold_mode)
        form.addRow(self.ignore_removed)
        form.addRow("Report an error after N failures", self.error_threshold)

    def _build_highlight(self) -> None:
        form = self._tab("Highlight")
        self.hl_mode = QComboBox()
        self.hl_mode.addItem("Standard: blocks aligned, moves ignored", "standard")
        self.hl_mode.addItem("Exact: moves count as changes", "exact")
        self.hl_mode.addItem("Table: rows are blocks", "table")
        self.hl_mode.setCurrentIndex(self.hl_mode.findData(self._orig.highlight_mode.value))
        form.addRow("Method", self.hl_mode)

    # -- Actions / Login / Advanced / Notes ---------------------------------------------

    def _build_actions(self) -> None:
        form = self._tab("Actions")
        current = {a.type.value: a.params for a in self._orig.actions.actions}
        self._action_params = dict(current)
        self.action_boxes: dict[str, QCheckBox] = {}
        for t in ActionType:
            label = t.value + (
                "" if t.value in IMPLEMENTED_ACTIONS else "  (not available in this build)"
            )
            box = QCheckBox(label)
            box.setChecked(t.value in current)
            self.action_boxes[t.value] = box
            form.addRow(box)
        self.privacy = QComboBox()
        self.privacy.addItem("Include the changes", "content")
        self.privacy.addItem("Private: only say which bookmark changed", "private")
        self.privacy.setCurrentIndex(self.privacy.findData(self._orig.actions.alert_privacy.value))
        form.addRow("Alert content", self.privacy)

    def _build_login(self) -> None:
        form = self._tab("Login")
        form.addRow(
            QLabel(
                "Signed-in checks (macros, cookies, semi-attended sign-in) are not available yet."
            )
        )

    def _build_advanced(self) -> None:
        f = self._orig.fetch
        form = self._tab("Advanced")
        self.http_method = QComboBox()
        self.http_method.addItems(["GET", "POST"])
        self.http_method.setCurrentText(f.method)
        self.headers = QPlainTextEdit("\n".join(f"{k}: {v}" for k, v in f.headers.items()))
        self.body = QPlainTextEdit(f.body or "")
        self.user_agent = QLineEdit(f.user_agent or "")
        self.proxy = QLineEdit(f.proxy or "")
        self.proxy.setPlaceholderText("http://host:port or socks5://host:port")
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setValue(f.timeout_s)
        self.verify = QCheckBox("Verify TLS certificates")
        self.verify.setChecked(f.verify_tls)
        form.addRow("HTTP method", self.http_method)
        form.addRow("Extra headers (Name: value)", self.headers)
        form.addRow("POST body", self.body)
        form.addRow("User agent", self.user_agent)
        form.addRow("Proxy", self.proxy)
        form.addRow("Timeout (s)", self.timeout)
        form.addRow(self.verify)

    def _build_notes(self) -> None:
        o = self._orig
        form = self._tab("Notes")
        self.info = [QLineEdit(v or "") for v in (o.info1, o.info2, o.info3)]
        self.note = QPlainTextEdit(o.note or "")
        for i, edit in enumerate(self.info, 1):
            form.addRow(f"Info {i}", edit)
        form.addRow("Note", self.note)

    # -- patch --------------------------------------------------------------------------

    def _selected(self, tab: str) -> bool:
        return not self.bulk or self._apply[tab].isChecked()

    def actions_dict(self) -> dict[str, Any]:
        return {
            "actions": [
                {"type": t, "params": self._action_params.get(t, {})}
                for t, box in self.action_boxes.items()
                if box.isChecked()
            ],
            "alert_privacy": self.privacy.currentData(),
        }

    def fetch_dict(self) -> dict[str, Any]:
        headers: dict[str, str] = {}
        for line in _lines(self.headers):
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip()] = value.strip()
        return {
            "method": self.http_method.currentText(),
            "headers": headers,
            "body": self.body.toPlainText() or None,
            "user_agent": self.user_agent.text().strip() or None,
            "proxy": self.proxy.text().strip() or None,
            "timeout_s": float(self.timeout.value()),
            "verify_tls": self.verify.isChecked(),
        }

    def gate_dict(self) -> dict[str, Any]:
        return {
            "min_chars": self.min_chars.value(),
            "blacklist": _lines(self.blacklist),
            "whitelist": _lines(self.whitelist),
            "min_changed_words": self.min_words.value(),
            "threshold_mode": self.threshold_mode.currentData(),
            "ignore_removed": self.ignore_removed.isChecked(),
            "error_threshold": self.error_threshold.value(),
            "keywords": self.keywords.toPlainText(),
            "highlight_keywords": self.highlight_keywords.toPlainText(),
        }

    def build_patch(self) -> dict[str, Any]:
        """What to send to ``PATCH /bookmarks``: single mode sends only the differences from
        the effective values; bulk mode sends the full values of the ticked tabs."""
        o = self._orig
        patch: dict[str, Any] = {}

        def section(name: str, tab: str, orig: dict[str, Any], new: dict[str, Any]) -> None:
            if not self._selected(tab):
                return
            d = new if self.bulk else diff_dict(orig, new)
            if d:
                patch[name] = d

        if self._selected("General"):
            if not self.bulk:
                if self.name.text() != o.name:
                    patch["name"] = self.name.text()
                if self.url.text().strip() != o.url:
                    patch["url"] = self.url.text().strip()
            folder = self.folder.currentData()
            if self.bulk or folder != o.folder_id:
                if folder is None:
                    patch["move_to_root"] = True
                else:
                    patch["folder_id"] = folder
            for key, value, before in (
                ("check_method", self.method.currentData(), o.check_method.value),
                ("source_type", self.source.currentData(), o.source_type.value),
                ("enabled", self.enabled.isChecked(), o.enabled),
                ("priority", int(self.hotsite.isChecked()), o.priority),
            ):
                if self.bulk or value != before:
                    patch[key] = value
        section("schedule", "Schedule", o.schedule.model_dump(mode="json"), self.schedule_dict())
        if self._selected("Filters"):
            new = self.filter_dict()
            orig = o.filter.model_dump(mode="json", exclude_defaults=True)
            for name in RULE_LISTS:
                orig[name] = [
                    r.model_dump(mode="json", exclude_defaults=True)
                    for r in getattr(o.filter, name)
                ]
            orig["builtin_cosmetic"] = o.filter.builtin_cosmetic
            orig["special"] = o.filter.special.model_dump(mode="json")
            d = new if self.bulk else diff_dict(orig, new)
            if d:
                patch["filter"] = d
        gate_new = self.gate_dict()
        gate_orig = o.gate.model_dump(mode="json")
        gate: dict[str, Any] = {}
        for tab, keys in (("Keywords", ("keywords", "highlight_keywords")),
                          ("Gate", tuple(k for k in gate_new if k not in ("keywords", "highlight_keywords")))):  # fmt: skip
            if self._selected(tab):
                part_new = {k: gate_new[k] for k in keys}
                gate |= part_new if self.bulk else diff_dict(gate_orig, part_new)
        if gate:
            patch["gate"] = gate
        if self._selected("Highlight") and (
            self.bulk or self.hl_mode.currentData() != o.highlight_mode.value
        ):
            patch["highlight_mode"] = self.hl_mode.currentData()
        if self._selected("Actions"):
            new_actions = self.actions_dict()
            orig_actions = {
                "actions": [{"type": a.type.value, "params": a.params} for a in o.actions.actions],
                "alert_privacy": o.actions.alert_privacy.value,
            }
            if self.bulk or new_actions != orig_actions:
                patch["actions"] = new_actions
        section("fetch", "Advanced", o.fetch.model_dump(mode="json"), self.fetch_dict())
        if self._selected("Notes"):
            for i, edit in enumerate(self.info, 1):
                before = getattr(o, f"info{i}") or ""
                if self.bulk or edit.text() != before:
                    patch[f"info{i}"] = edit.text() or None
            if self.bulk or self.note.toPlainText() != (o.note or ""):
                patch["note"] = self.note.toPlainText() or None
        return patch

    # -- save ---------------------------------------------------------------------------

    def save(self) -> None:
        try:
            patch = self.build_patch()
        except ValidationError as exc:
            self.error.setText(f"Invalid filter rule: {exc.errors()[0]['msg']}")
            return
        self.error.setText("")
        if not patch:
            self.accept()
            return
        ids = [b.id for b in self._all]

        def work() -> int:
            if self.bulk:
                return self._client.bulk(ids, "update", patch=patch)
            self._client.patch_bookmark(ids[0], patch)
            return 1

        def done(_n: int) -> None:
            self.saved.emit()
            self.accept()

        def failed(exc: Exception) -> None:
            message = str(exc).split(": ", 1)[-1]
            self.error.setText(message)
            self.save_failed.emit(message)

        run_async(work, done, failed)
