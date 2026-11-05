"""M7: the Filter Assistant in the UI: the QWebChannel bridge in a real web view (Alt+select, right
click, the shortcut), the popup with each choice's Test filter result, and the whole flow through the
main window against a real engine."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.filter_assistant import FilterAssistantDialog, describe_rule
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.viewer import BRIDGE_WORLD, T_NEW, T_TEXT
from tests.support.engine_thread import EngineThread
from tests.ui.test_main_window import open_window, rendered  # noqa: F401  (fixture + helper)

K = Qt.Key
SCHED = {"interval_s": 60, "jitter_pct": 0}
STAMP = "/html[1]/body[1]/p[1]"
LI1 = "/html[1]/body[1]/ul[1]/li[1]"
LI2 = "/html[1]/body[1]/ul[1]/li[2]"


def page(minutes: int = 5, extra: str = "", visitors: str = "1,234") -> str:
    return (
        "<html><head><title>Digest</title></head><body><h1>Digest</h1>"
        f'<p class="stamp">Updated {minutes} minutes ago</p>'
        f"<ul><li>Story one</li><li>Story two</li>{extra}</ul>"
        f"<footer>Visitors today: {visitors}</footer></body></html>"
    )


def unread_bookmark(eng: EngineThread, client: ApiClient) -> int:
    eng.site.set("/d", page())
    out = client.create_bookmark({"url": eng.site.url("/d"), "name": "Digest", "schedule": SCHED})
    eng.settle()
    eng.site.set("/d", page(minutes=6, extra="<li>Story three</li>"))
    eng.advance(70)
    assert client.counts().unread == 1
    return out.id


def show(w: MainWindow, pump_until: Callable[..., None], tab_key: Any = None) -> None:
    pump_until(lambda: w.model.rowCount() == 1, what="list loaded")
    rendered(w, pump_until, lambda: QTest.keyClick(w, K.Key_N))
    if tab_key is not None:
        rendered(
            w,
            pump_until,
            lambda: QTest.keyClick(w, tab_key, Qt.KeyboardModifier.ControlModifier),
            T_NEW,
        )


def bridge_only(w: MainWindow) -> None:
    """These tests look at what the bridge reports; they must not open the (modal) popup."""
    w.viewer.selection_made.disconnect(w._open_assistant)


def run_js(w: MainWindow, js: str) -> None:
    w.viewer.web.page().runJavaScript(js, BRIDGE_WORLD)


SELECT = """
(function () {{
  var el = document.querySelector({selector!r});
  var r = document.createRange(); r.selectNodeContents(el);
  var s = getSelection(); s.removeAllRanges(); s.addRange(r);
  {fire}
}})();
"""
ALT_UP = (
    "document.dispatchEvent(new MouseEvent('mouseup', {altKey: true, bubbles: true, button: 0}));"
)
PLAIN_UP = "document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}));"


def select_js(selector: str, fire: str = ALT_UP) -> str:
    return SELECT.format(selector=selector, fire=fire)


def wait_for_selection(
    w: MainWindow, pump_until: Callable[..., None], js: str, attempts: int = 40
) -> dict[str, Any]:
    """Run ``js`` until the bridge reports (it connects a moment after the page loads)."""
    got: list[dict[str, Any]] = []
    w.viewer.selection_made.connect(got.append)
    for _ in range(attempts):
        run_js(w, js)
        end = time.monotonic() + 0.4
        while time.monotonic() < end and not got:
            QApplication.processEvents()
            time.sleep(0.01)
        if got:
            return got[-1]
    raise AssertionError("the bridge never reported a selection")


def stays_quiet(w: MainWindow, js: str) -> bool:
    got: list[dict[str, Any]] = []
    w.viewer.selection_made.connect(got.append)
    run_js(w, js)
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return not got


# -- the bridge ---------------------------------------------------------------------------------


def test_alt_select_reports_the_text_and_its_dom_path(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    bridge_only(w)
    sel = wait_for_selection(w, pump_until, select_js("p.stamp"))
    assert sel == {
        "via": "alt", "text": "Updated 6 minutes ago", "path": STAMP, "end_path": None,
        "bookmark_id": bid, "version_id": None,
    }  # fmt: skip
    assert w.viewer.web.bridge_ready
    # without Alt, selecting text is just selecting text
    assert stays_quiet(w, select_js("p.stamp", PLAIN_UP))


def test_a_selection_across_elements_reports_where_it_starts_and_ends(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    bridge_only(w)
    span = """
    (function () {
      var a = document.querySelectorAll('li');
      var r = document.createRange();
      r.setStart(a[0].firstChild, 6); r.setEnd(a[1].firstChild, 5);
      var s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.dispatchEvent(new MouseEvent('mouseup', {altKey: true, bubbles: true, button: 0}));
    })();
    """
    sel = wait_for_selection(w, pump_until, span)
    assert (sel["path"], sel["end_path"]) == (LI1, LI2)
    assert "one" in sel["text"] and "Story" in sel["text"]
    # a triple-click ends at the start of the NEXT block; the bridge moves it back
    triple = """
    (function () {
      var li = document.querySelector('li');
      var r = document.createRange();
      r.setStart(li.firstChild, 0); r.setEnd(li.nextElementSibling, 0);
      var s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.dispatchEvent(new MouseEvent('mouseup', {altKey: true, bubbles: true, button: 0}));
    })();
    """
    sel = wait_for_selection(w, pump_until, triple)
    assert (sel["path"], sel["end_path"]) == (LI1, None) and sel["text"].strip() == "Story one"


def test_right_click_works_on_a_selection_and_on_a_bare_element(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    bridge_only(w)
    ctx = (
        "document.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));"
    )
    sel = wait_for_selection(w, pump_until, select_js("li:nth-child(2)", ctx))
    assert sel["via"] == "context" and sel["text"] == "Story two" and sel["path"] == LI2
    # no selection: the element under the pointer is what is meant
    bare = """
    (function () {
      getSelection().removeAllRanges();
      var ev = new MouseEvent('contextmenu', {bubbles: true, cancelable: true});
      document.querySelector('p.stamp').dispatchEvent(ev);
    })();
    """
    sel = wait_for_selection(w, pump_until, bare)
    assert sel["text"] == "" and sel["path"] == STAMP and sel["end_path"] is None


def test_the_bridge_is_inert_where_there_are_no_paths_and_invisible_to_the_page(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    bridge_only(w)
    wait_for_selection(w, pump_until, select_js("p.stamp"))  # it works on Highlighted
    rendered(
        w,
        pump_until,
        lambda: QTest.keyClick(w, K.Key_2, Qt.KeyboardModifier.ControlModifier),
        T_TEXT,
    )
    assert "data-pw-path" not in w.viewer.web.last_html
    assert stays_quiet(w, select_js(".pw-b"))  # the Text diff view has no paths: nothing happens
    # the page's own world cannot see the bridge (scripts are stripped anyway)
    seen: list[Any] = []
    w.viewer.web.page().runJavaScript("typeof window.__pwReport + typeof window.qt", seen.append)
    pump_until(lambda: bool(seen), what="main world answer")
    assert seen == ["undefinedundefined"]


def test_the_shortcut_uses_the_current_selection_or_says_what_to_do(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    bridge_only(w)
    wait_for_selection(w, pump_until, select_js("p.stamp"))  # the bridge is up
    opened: list[dict[str, Any]] = []
    w.viewer.selection_made.connect(opened.append)
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    run_js(w, select_js("li", "0;"))  # a selection exists, but nobody pressed Alt
    QTest.keyClick(w, K.Key_F, ctrl_shift)
    pump_until(lambda: bool(opened), what="shortcut report")
    assert opened[0]["via"] == "shortcut" and opened[0]["path"] == LI1
    run_js(w, "getSelection().removeAllRanges();")
    QApplication.processEvents()
    QTest.keyClick(w, K.Key_F, ctrl_shift)
    pump_until(lambda: "Select some text first" in w.lbl_view.text(), what="hint")
    rendered(
        w,
        pump_until,
        lambda: QTest.keyClick(w, K.Key_2, Qt.KeyboardModifier.ControlModifier),
        T_TEXT,
    )
    QTest.keyClick(w, K.Key_F, ctrl_shift)
    pump_until(lambda: "Highlighted and New" in w.lbl_view.text(), what="wrong tab hint")


# -- the popup ----------------------------------------------------------------------------------


def make_dialog(
    client: ApiClient, bid: int, path: str, text: str, **extra: Any
) -> FilterAssistantDialog:
    body = {"path": path, "text": text, **extra}
    return FilterAssistantDialog(client, bid, body, client.filter_assist(bid, body))


def test_every_choice_shows_its_test_result_before_anything_is_saved(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    d = make_dialog(api_client, bid, STAMP, "Updated 6 minutes ago")
    assert [d.choices.item(i).text() for i in range(d.choices.count())] == [
        "Ignore this element", "Watch only this element",
        "Ignore text like this (numbers generalized)", "Ignore from page start to here",
        "Ignore from here to page end",
    ]  # fmt: skip
    assert d.selection.text() == "“Updated 6 minutes ago”" and "p.stamp" in d.where.text()
    assert d.choices.currentRow() == 0 and d.selected_choice().id == "ignore_element"  # type: ignore[union-attr]

    pump_until(lambda: d.test_result("ignore_element") is not None, what="first test")
    pump_until(lambda: "Testing" not in d.test_summary.text(), what="first test shown")
    assert "(now:" in d.test_summary.text()  # the baseline arrived and is compared with
    # hiding the stamp does not hide the other change (Story three was added): it still alerts
    assert (
        "1 changed block(s) remain" in d.test_summary.text()
        and "would alert" in d.test_summary.text()
    )
    d.select("ignore_text")
    assert d.rule_line.text().startswith("ignore → text (regex)")
    assert "Updated\\s+\\d+\\s+minutes\\s+ago" in d.rule_line.text()
    pump_until(lambda: d.test_result("ignore_text") is not None, what="text test")
    assert "Page text after this filter" in d.test_summary.text()
    assert "Story three" in d.test_text.toPlainText() and "Updated" not in d.test_text.toPlainText()
    d.select("watch_element")
    pump_until(lambda: d.test_result("watch_element") is not None, what="watch test")
    assert d.test_text.toPlainText().strip() == "Updated 6 minutes ago"
    assert "block" in d.test_summary.text() and "(now:" in d.test_summary.text()
    d.select("ignore_from_here")
    pump_until(lambda: d.test_result("ignore_from_here") is not None, what="marker test")
    assert "between" in d.rule_line.text() and "to the end of the page" in d.rule_line.text()
    # nothing was saved by looking
    assert (
        api_client.bookmark(bid).filter.ignore == [] and api_client.bookmark(bid).filter.watch == []
    )
    d.select("ignore_text")
    assert d.patch()["filter"]["ignore"][0]["pattern"] == r"Updated\s+\d+\s+minutes\s+ago"
    d.deleteLater()


def test_widening_to_the_parent_and_warnings(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    d = make_dialog(api_client, bid, LI2, "")  # nothing selected: the element is meant
    assert (
        "<li>" in d.where.text()
        and d.levels.isEnabled()
        and "No text was selected"
        in "".join(c.warnings[0] for c in d.result.choices if c.id == "ignore_text")
    )
    d.select("ignore_element")
    d.levels.setValue(1)
    pump_until(lambda: "<ul>" in d.where.text(), what="parent element")
    assert d.selected_choice().id == "ignore_element"  # type: ignore[union-attr]  # kept
    assert d.request["levels_up"] == 1
    d.levels.setValue(9)  # as far as it goes: the body
    pump_until(lambda: "<body>" in d.where.text(), what="body")
    assert not d.levels.isEnabled() or d.result.element.can_widen is False  # type: ignore[union-attr]
    pump_until(lambda: "whole page" in d.warning.text(), what="whole-page warning")
    d.deleteLater()


def test_a_failing_test_or_assist_call_is_explained_not_raised(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    d = make_dialog(api_client, bid, STAMP, "Updated 6 minutes ago")
    pump_until(lambda: d.test_result("ignore_element") is not None, what="baseline")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("engine said no")

    monkeypatch.setattr(api_client, "test_filter", boom)
    d.select("watch_element")
    pump_until(lambda: "could not run" in d.test_summary.text(), what="error text")
    assert "engine said no" in d.test_summary.text()
    d.deleteLater()


def test_the_verdict_says_when_a_change_disappears_or_stays() -> None:
    from pagewatch.models import TestFilterOut
    from pagewatch.ui.windows.filter_assistant import verdict_text

    def out(latest: list[str], identical: bool, alert: bool = False, reason: str | None = None,
            changed: int = 0, warnings: list[str] | None = None) -> TestFilterOut:  # fmt: skip
        return TestFilterOut(
            baseline=[], latest=latest, marks=[], diff={"stats": {"changed_blocks": changed}},
            alert=alert, reason=reason, keyword_hits=[], identical=identical,
            warnings=warnings or [],
        )  # fmt: skip

    before = out(["one two", "three", "four five six"], identical=False, alert=True, changed=1)
    gone = verdict_text(before, out(["one two", "three"], identical=True))
    assert gone.splitlines()[0] == (
        "Page text after this filter: 2 blocks, 3 words (now: 3 blocks, 6 words)"
    )
    assert "disappears" in gone
    quiet = verdict_text(out(["a"], identical=True), out(["a"], identical=True))
    assert "Nothing differs" in quiet and "disappears" not in quiet
    stays = verdict_text(
        before, out(["a"], identical=False, alert=False, reason="keyword_miss", changed=2)
    )
    assert "2 changed block(s) remain" in stays and "would not alert (keyword_miss)" in stays
    noted = verdict_text(None, out(["a"], identical=True, warnings=["a regex was skipped"]))
    assert "(now:" not in noted and "Note: a regex was skipped" in noted


def test_rules_are_described_in_words() -> None:
    from pagewatch.models import FilterRule

    assert describe_rule(FilterRule(type="selector", selector="p.a"), "ignore") == (
        "ignore → CSS selector  p.a"
    )
    assert "XPATH selector" in describe_rule(
        FilterRule(type="selector", selector="/html[1]/body[1]", selector_kind="xpath"), "watch"
    )
    both = describe_rule(FilterRule(type="between", start="A", end="B", inclusive=True), "ignore")
    assert both == "ignore → between  from “A” to “B” (inclusive)"


# -- through the main window ---------------------------------------------------------------------


def drive(
    w: MainWindow, pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch, choice: str
) -> list[FilterAssistantDialog]:
    """Make the next assistant dialog pick ``choice`` and Save, once its test result is in."""
    seen: list[FilterAssistantDialog] = []

    def fake_exec(dialog: QDialog) -> bool:
        assert isinstance(dialog, FilterAssistantDialog)
        seen.append(dialog)
        dialog.select(choice)
        pump_until(lambda: dialog.test_result(choice) is not None, what="test result")
        return True

    monkeypatch.setattr(w, "_exec", fake_exec)
    return seen


def test_alt_select_to_saved_filter_stops_the_false_positive(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    seen = drive(w, pump_until, monkeypatch, "ignore_text")
    wait_for_selection(w, pump_until, select_js("p.stamp"))
    pump_until(lambda: bool(seen), what="the popup")
    assert len(seen) == 1  # one popup per selection, not one per retry
    pump_until(lambda: len(api_client.bookmark(bid).filter.ignore) == 1, what="filter saved")
    rule = api_client.bookmark(bid).filter.ignore[0]
    assert rule.type == "text" and rule.pattern == r"Updated\s+\d+\s+minutes\s+ago"
    pump_until(lambda: "Filter saved" in w.lbl_message.text(), what="status message")
    # the filter is live: the next timestamp tick is not a change
    api_client.mark_read(bid)
    eng.site.set("/d", page(minutes=40, extra="<li>Story three</li>"))
    eng.advance(70)
    assert api_client.counts().unread == 0
    eng.site.set("/d", page(minutes=41, extra="<li>Story three</li><li>Story four</li>"))
    eng.advance(70)
    assert api_client.counts().unread == 1


def test_ignore_this_element_from_a_right_click_and_cancel_saves_nothing(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    w = open_window()
    show(w, pump_until)
    bare = """
    (function () {
      getSelection().removeAllRanges();
      document.querySelector('footer').dispatchEvent(
        new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
    })();
    """
    seen = drive(w, pump_until, monkeypatch, "ignore_element")
    wait_for_selection(w, pump_until, bare)
    pump_until(lambda: bool(seen), what="popup")
    pump_until(lambda: len(api_client.bookmark(bid).filter.ignore) == 1, what="saved")
    assert api_client.bookmark(bid).filter.ignore[0].selector == "footer"

    # cancelling: the dialog is rejected, nothing is saved
    before = api_client.bookmark(bid).filter.ignore
    monkeypatch.setattr(w, "_exec", lambda dialog: False)
    run_js(w, bare)
    time.sleep(0.5)
    for _ in range(30):
        QApplication.processEvents()
        time.sleep(0.01)
    assert api_client.bookmark(bid).filter.ignore == before


def test_a_selection_in_an_older_alerts_page_is_looked_up_in_that_page(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = unread_bookmark(eng, api_client)
    eng.site.set("/d", page(minutes=7, extra="<li>Story three</li>"))
    eng.advance(70)  # a second alert: two entries in the history list
    w = open_window()
    show(w, pump_until)
    pump_until(lambda: w.viewer.history.count() == 3, what="history")
    rendered(w, pump_until, lambda: w.viewer.history.setCurrentIndex(2))  # the older alert
    sel = wait_for_selection(w, pump_until, select_js("p.stamp"))
    changes = api_client.changes(bid).items
    older = min(changes, key=lambda c: c.id)
    assert sel["version_id"] == older.new_version_id
    seen = drive(w, pump_until, monkeypatch, "watch_element")
    w._open_assistant(sel)
    pump_until(lambda: bool(seen), what="popup")
    assert "6 minutes" in seen[0].selection.text() or "6 minutes" in seen[0].test_text.toPlainText()
