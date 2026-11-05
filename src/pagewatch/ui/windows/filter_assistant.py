"""The filter assistant popup (spec: Desktop UI → Filter assistant).

The user selected text in the Highlighted or New view; the engine offered up to five filters for
it. Each choice shows its Test filter result before anything is saved: how much of the page is
left, and what happens to the change the user is looking at. Nothing is stored until Save; the
caller then sends ``patch()`` to ``PATCH /bookmarks/{id}``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from pagewatch.models import FilterAssistOut, FilterChoice, FilterRule, TestFilterOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.workers import run_async

PREVIEW_BLOCKS = 40
PREVIEW_CHARS = 160


def describe_rule(rule: FilterRule, section: str) -> str:
    """One line a person can read: which list the rule goes to and what it matches."""
    if rule.type == "selector":
        what = f"{rule.selector_kind.upper()} selector  {rule.selector}"
    elif rule.type == "text":
        what = f"text ({rule.pattern_kind})  {rule.pattern}"
    elif rule.type == "between":
        parts = []
        if rule.start is not None:
            parts.append(f"from “{rule.start}”")
        else:
            parts.append("from the start of the page")
        if rule.end is not None:
            parts.append(f"to “{rule.end}”")
        else:
            parts.append("to the end of the page")
        what = "between  " + " ".join(parts) + (" (inclusive)" if rule.inclusive else "")
    else:
        what = rule.type
    return f"{section} → {what}"


def _words(blocks: list[str]) -> int:
    return sum(len(b.split()) for b in blocks)


def _shorten(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def verdict_text(before: TestFilterOut | None, after: TestFilterOut) -> str:
    """What the candidate does to the change the user is looking at."""
    n_blocks, n_words = len(after.latest), _words(after.latest)
    lines = [f"Page text after this filter: {n_blocks} blocks, {n_words} words"]
    if before is not None:
        lines[0] += f" (now: {len(before.latest)} blocks, {_words(before.latest)} words)"
    if after.identical:
        if before is not None and not before.identical:
            lines.append("The change you are looking at disappears: nothing differs any more.")
        else:
            lines.append("Nothing differs between the last page you read and the latest.")
    else:
        changed = after.diff.get("stats", {}).get("changed_blocks", 0)
        verdict = "would alert" if after.alert else f"would not alert ({after.reason})"
        lines.append(f"{changed} changed block(s) remain between the last read page and the latest; "
                     f"this {verdict}.")  # fmt: skip
    for w in after.warnings:
        lines.append(f"Note: {w}")
    return "\n".join(lines)


class FilterAssistantDialog(QDialog):
    def __init__(
        self,
        client: ApiClient,
        bookmark_id: int,
        request: dict[str, Any],
        result: FilterAssistOut,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._bid = bookmark_id
        self.request = dict(request)
        self.result = result
        self._tests: dict[str, TestFilterOut] = {}  # by choice id, for the current choices
        self._inflight: set[str] = set()
        self._before: TestFilterOut | None = None
        self._gen = 0  # bumped when the choices are rebuilt: older answers no longer apply
        self.setWindowTitle("Filter assistant")
        self.resize(640, 560)

        self.selection = QLabel()
        self.selection.setObjectName("assistSelection")
        self.selection.setWordWrap(True)
        self.where = QLabel()
        self.where.setObjectName("assistWhere")
        self.levels = QSpinBox()
        self.levels.setObjectName("assistLevels")
        self.levels.setRange(0, 12)
        self.levels.setValue(int(self.request.get("levels_up", 0)))
        self.levels.setToolTip("Use a parent of the selected element instead")
        self.choices = QListWidget()
        self.choices.setObjectName("assistChoices")
        self.explain = QLabel()
        self.explain.setObjectName("assistExplain")
        self.explain.setWordWrap(True)
        self.rule_line = QLabel()
        self.rule_line.setObjectName("assistRule")
        self.rule_line.setWordWrap(True)
        self.rule_line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warning = QLabel()
        self.warning.setObjectName("assistWarning")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b36b00")
        self.test_summary = QLabel("Testing…")
        self.test_summary.setObjectName("assistTestSummary")
        self.test_summary.setWordWrap(True)
        self.test_text = QPlainTextEdit()
        self.test_text.setObjectName("assistTestText")
        self.test_text.setReadOnly(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Selected", self.selection)
        form.addRow("In", self.where)
        form.addRow("Use a parent element", self.levels)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("What do you want to do with it?"))
        layout.addWidget(self.choices)
        layout.addWidget(self.explain)
        layout.addWidget(self.rule_line)
        layout.addWidget(self.warning)
        layout.addWidget(QLabel("Test filter result"))
        layout.addWidget(self.test_summary)
        layout.addWidget(self.test_text, 1)
        layout.addWidget(self.buttons)

        self.choices.currentRowChanged.connect(self._choice_changed)
        self.levels.valueChanged.connect(self._widen)
        self._populate(None)
        self._baseline()

    # -- what is on offer ---------------------------------------------------------------

    def selected_choice(self) -> FilterChoice | None:
        row = self.choices.currentRow()
        return self.result.choices[row] if 0 <= row < len(self.result.choices) else None

    def select(self, choice_id: str) -> None:
        for i, c in enumerate(self.result.choices):
            if c.id == choice_id:
                self.choices.setCurrentRow(i)
                return

    def patch(self) -> dict[str, Any]:
        """The ``PATCH /bookmarks/{id}`` body that adds the selected rule."""
        choice = self.selected_choice()
        return dict(choice.patch) if choice is not None else {}

    def _populate(self, keep: str | None) -> None:
        r = self.result
        self.selection.setText(f"“{_shorten(r.selection, 200)}”" if r.selection else "(nothing)")
        el = r.element
        if el is None:
            self.where.setText("(the element could not be found; text-based filters only)")
            self.levels.setEnabled(False)
        else:
            self.where.setText(f"<{el.tag}>  {el.selector}")
            self.levels.setEnabled(el.can_widen or el.levels_up > 0)
        self.choices.blockSignals(True)
        self.choices.clear()
        for c in r.choices:
            item = QListWidgetItem(c.label)
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            self.choices.addItem(item)
        self.choices.blockSignals(False)
        self._gen += 1
        self._tests = {}
        self._inflight = set()
        order = [c.id for c in r.choices]
        self.choices.setCurrentRow(order.index(keep) if keep in order else 0)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(r.choices))

    def _widen(self, levels: int) -> None:
        """Ask the engine again with the parent element ``levels`` steps up."""
        keep = self.selected_choice().id if self.selected_choice() else None
        body = {**self.request, "levels_up": levels}
        self.test_summary.setText("Looking up the parent element…")

        def done(out: FilterAssistOut) -> None:
            self.request = body
            self.result = out
            self.levels.blockSignals(True)
            self.levels.setValue(out.element.levels_up if out.element else 0)
            self.levels.blockSignals(False)
            self._populate(keep)

        run_async(
            lambda: self._client.filter_assist(self._bid, body),
            done,
            lambda exc: self.test_summary.setText(str(exc).split(": ", 1)[-1]),
        )

    # -- testing a choice ---------------------------------------------------------------

    def _test_body(self, candidate: dict[str, Any]) -> dict[str, Any]:
        body = dict(candidate)
        if self.request.get("version_id") is not None:
            body["to_version_id"] = self.request["version_id"]
        return body

    def _baseline(self) -> None:
        """The page and verdict as they are now, to compare each candidate with."""

        def done(out: TestFilterOut) -> None:
            self._before = out
            cached = self._tests.get(self._current_id())
            if cached is not None:  # a result that arrived first can now say "(now: ...)"
                self._show_test(cached)

        run_async(
            lambda: self._client.test_filter(self._bid, self._test_body({})), done, lambda _e: None
        )

    def _current_id(self) -> str | None:
        choice = self.selected_choice()
        return choice.id if choice is not None else None

    def _choice_changed(self, row: int) -> None:
        choice = self.selected_choice()
        if choice is None:
            return
        self.explain.setText(choice.explanation)
        self.rule_line.setText(describe_rule(choice.rule, choice.section))
        self.warning.setText("\n".join(choice.warnings))
        cached = self._tests.get(choice.id)
        if cached is not None:
            self._show_test(cached)
            return
        self.test_summary.setText("Testing\u2026")
        self.test_text.setPlainText("")
        if choice.id in self._inflight:
            return  # already asked; the answer will be shown when it arrives
        self._inflight.add(choice.id)
        gen = self._gen

        def done(out: TestFilterOut) -> None:
            if gen != self._gen:  # the choices were rebuilt (a parent element): stale
                return
            self._inflight.discard(choice.id)
            self._tests[choice.id] = out
            if self._current_id() == choice.id:
                self._show_test(out)

        def failed(exc: Exception) -> None:
            if gen != self._gen:
                return
            self._inflight.discard(choice.id)
            if self._current_id() == choice.id:
                self.test_summary.setText(f"The test could not run: {str(exc).split(': ', 1)[-1]}")

        body = self._test_body({"filter": choice.patch["filter"]})
        run_async(lambda: self._client.test_filter(self._bid, body), done, failed)

    def _show_test(self, out: TestFilterOut) -> None:
        self.test_summary.setText(verdict_text(self._before, out))
        shown = [_shorten(b, PREVIEW_CHARS) for b in out.latest[:PREVIEW_BLOCKS]]
        more = len(out.latest) - len(shown)
        text = "\n".join(shown) + (f"\n\u2026 and {more} more block(s)" if more > 0 else "")
        self.test_text.setPlainText(text or "(the page would have no text left)")

    def test_result(self, choice_id: str) -> TestFilterOut | None:
        """The Test filter result already fetched for a choice (for tests and the caller)."""
        return self._tests.get(choice_id)
