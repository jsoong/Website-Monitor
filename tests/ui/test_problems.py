"""M5: the Problems window, against a real engine (real HTTP and WebSocket, fake clock)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from pagewatch.engine.actions.base import AlertContext
from pagewatch.engine.actions.builtin import ACTIONS
from pagewatch.engine.core import Engine
from pagewatch.models import ProblemOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.problems import (
    COL_KIND,
    COL_NAME,
    COL_TRIES,
    COL_WHY,
    ProblemsWindow,
    explain,
    when_text,
)
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article


def problem(kind: str, message: str, **kw: Any) -> ProblemOut:
    base: dict[str, Any] = {
        "kind": kind, "bookmark_id": 1, "bookmark_name": "Shop", "url": "http://x.test/",
        "message": message, "occurred_at": "2026-03-01T12:30:00Z",
    }  # fmt: skip
    return ProblemOut.model_validate({**base, **kw})


def test_explain_turns_failure_codes_into_sentences_and_keeps_unknown_ones() -> None:
    assert explain(problem("bookmark_error", "http_503")) == "The server answered HTTP 503"
    assert "certificate" in explain(problem("bookmark_error", "tls"))
    assert "DNS" in explain(problem("bookmark_error", "dns"))
    assert explain(problem("needs_login", "needs login")).startswith("The site wants you")
    assert explain(problem("bookmark_error", "weird_new_code")) == "weird_new_code"
    action = problem("action_failed", "smtp.example.test refused the connection")
    assert explain(action) == "smtp.example.test refused the connection"  # already a sentence


def test_when_text_is_local_time_and_survives_garbage() -> None:
    assert when_text(None) == "" and when_text("") == ""
    assert when_text("not a time") == "not a time"
    assert len(when_text("2026-03-01T12:30:00Z")) == len("2026-03-01 12:30")


def make(eng: EngineThread, client: ApiClient, name: str, **kw: Any) -> int:
    """A bookmark that has had its first good check and turns `error` on its first failure."""
    path = "/" + name.lower().replace(" ", "-")
    eng.site.set(path, article(f"{name} tea", title=name))
    out = client.create_bookmark(
        {
            "url": eng.site.url(path), "name": name, "gate": {"error_threshold": 1},
            "schedule": {"interval_s": 60, "jitter_pct": 0}, **kw,
        }
    )  # fmt: skip
    eng.settle()
    return out.id


def break_site(eng: EngineThread, name: str) -> None:
    eng.site.set("/" + name.lower().replace(" ", "-"), "gone", status=404)
    eng.advance(70)


def fix_site(eng: EngineThread, client: ApiClient, bid: int, name: str) -> None:
    eng.site.set("/" + name.lower().replace(" ", "-"), article(f"{name} tea", title=name))
    client.check([bid], force=True)
    eng.settle()


def cell(w: ProblemsWindow, row: int, col: int) -> str:
    item = w.table.item(row, col)
    return item.text() if item is not None else ""


@pytest.fixture
def open_main(
    eng: EngineThread, qapp: Any, pump_until: Callable[..., None]
) -> Iterator[Callable[[], MainWindow]]:
    windows: list[MainWindow] = []

    def factory() -> MainWindow:
        client = ApiClient(eng.base, eng.engine.token)
        events = EventStream(client.ws_url, client.token)
        w = MainWindow(client, events)
        events.start()
        w.show()
        pump_until(lambda: events.connected, what="event stream")
        windows.append(w)
        return w

    yield factory
    for w in windows:
        w.close()


def test_an_empty_list_says_so_and_a_failing_bookmark_appears_with_its_error(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = make(eng, api_client, "Shop")
    w = ProblemsWindow(api_client)
    counts: list[int] = []
    w.count_changed.connect(counts.append)
    pump_until(lambda: counts == [0], what="first load")
    assert w.empty.isHidden() is False and w.table.isHidden() and w.summary.text() == ""
    assert not any(b.isEnabled() for b in (w.btn_check, w.btn_show, w.btn_open, w.btn_copy))

    break_site(eng, "Shop")
    w.refresh()
    pump_until(lambda: w.total == 1, what="the problem")
    assert w.empty.isHidden() and not w.table.isHidden() and w.summary.text() == "1 problem"
    assert cell(w, 0, COL_KIND) == "Failing" and cell(w, 0, COL_NAME) == "Shop"
    assert cell(w, 0, COL_WHY) == "The server answered HTTP 404" and cell(w, 0, COL_TRIES) == "1"
    assert "http_404" in w.table.item(0, COL_WHY).toolTip()  # the raw code is still there
    assert not w.btn_check.isEnabled()  # nothing selected yet
    w.table.selectRow(0)
    assert all(b.isEnabled() for b in (w.btn_check, w.btn_show, w.btn_open, w.btn_copy))
    assert w.selected_problem() is not None and w.selected_problem().bookmark_id == bid  # type: ignore[union-attr]


def test_it_updates_itself_from_engine_events_and_clears_when_the_site_recovers(
    eng: EngineThread, api_client: ApiClient, open_main: Callable[[], MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    bid = make(eng, api_client, "Shop")
    main = open_main()
    pump_until(lambda: main.btn_problems.text() == "Problems: 0", what="count")
    main.open_problems()
    win = main.problems_window
    assert win is not None and win.isVisible()

    break_site(eng, "Shop")  # no manual refresh: the engine's `problem` event does it
    pump_until(lambda: win.total == 1 and win.table.rowCount() == 1, what="live update")
    assert main.btn_problems.text() == "Problems: 1" and "bold" in main.btn_problems.styleSheet()

    fix_site(eng, api_client, bid, "Shop")
    pump_until(lambda: win.total == 0 and win.table.rowCount() == 0, what="cleared")
    assert main.btn_problems.text() == "Problems: 0" and main.btn_problems.styleSheet() == ""
    assert win.empty.isHidden() is False


def test_a_failed_action_is_listed_with_its_attempts_and_cannot_be_checked_again(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    async def down(engine: Engine, ctx: AlertContext) -> None:
        raise RuntimeError("smtp.example.test refused the connection")

    monkeypatch.setitem(ACTIONS, "email", down)
    bid = make(eng, api_client, "Mailer", actions={"actions": [{"type": "email"}]})
    eng.site.set("/mailer", article("Mailer tea", "Mailer news", title="Mailer"))
    eng.advance(70)
    eng.advance((1 + 5 + 30 + 30) * 60 + 120, step=30)  # all five attempts

    w = ProblemsWindow(api_client)
    pump_until(lambda: w.total == 1 and w.table.rowCount() == 1, what="the failed action")
    assert cell(w, 0, COL_KIND) == "Action failed (email)" and cell(w, 0, COL_TRIES) == "5"
    assert cell(w, 0, COL_WHY) == "RuntimeError: smtp.example.test refused the connection"
    w.table.selectRow(0)
    assert not w.btn_check.isEnabled()  # there is nothing to "check" for an action
    assert w.btn_show.isEnabled() and w.selected_problem().bookmark_id == bid  # type: ignore[union-attr]

    api_client.mark_read(bid)  # reading the change acknowledges it
    w.refresh()
    pump_until(lambda: w.total == 0, what="acknowledged")


def test_check_now_open_url_and_copy_error(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    bid = make(eng, api_client, "Shop")
    break_site(eng, "Shop")
    w = ProblemsWindow(api_client)
    pump_until(lambda: w.total == 1 and w.table.rowCount() == 1, what="the problem")
    w.table.selectRow(0)

    before = len(eng.site.hits_for("/shop"))
    w.btn_check.click()
    pump_until(lambda: "Checking Shop" in w.message.text(), what="check queued")
    eng.settle()
    assert len(eng.site.hits_for("/shop")) > before  # the engine really fetched it again
    assert api_client.bookmark(bid).consecutive_errors >= 1

    opened: list[str] = []
    monkeypatch.setattr(w, "_open_external", opened.append)
    w.btn_open.click()
    assert opened == [eng.site.url("/shop")]

    w.btn_copy.click()
    text = QGuiApplication.clipboard().text()
    assert text.splitlines() == ["Shop", eng.site.url("/shop"), "http_404"]
    assert w.message.text() == "Copied"


def test_show_bookmark_selects_the_row_even_from_another_folder_and_search(
    eng: EngineThread, api_client: ApiClient, open_main: Callable[[], MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    ids = {n: make(eng, api_client, n) for n in ("Alpha", "Beta", "Gamma")}
    break_site(eng, "Beta")
    main = open_main()
    pump_until(lambda: main.model.rowCount() == 3, what="list loaded")
    main.search.setText("Alpha")  # the list is filtered to something else ...
    pump_until(lambda: main.model.rowCount() == 1, what="filtered")
    main.tree.setCurrentItem(main.tree._builtin_items["unread"])  # ... inside a folder with no rows
    pump_until(lambda: main.model.rowCount() == 0, what="nothing unread")

    main.open_problems()
    win = main.problems_window
    assert win is not None
    pump_until(lambda: win.table.rowCount() == 1, what="problems loaded")
    win.table.selectRow(0)
    win.btn_show.click()
    pump_until(
        lambda: main.current_summary() is not None and main.current_summary().id == ids["Beta"],  # type: ignore[union-attr]
        what="Beta selected in the main list",
    )
    assert main.search.text() == eng.site.url("/beta")
    QTest.qWait(50)
    assert main.current_summary().id == ids["Beta"]  # type: ignore[union-attr]
    assert main._reveal is None


def test_the_problems_shortcut_and_toolbar_action_open_the_window(
    eng: EngineThread, open_main: Callable[[], MainWindow], pump_until: Callable[..., None]
) -> None:
    main = open_main()
    assert main.problems_window is None
    main.act_problems.trigger()
    assert main.problems_window is not None and main.problems_window.isVisible()
    assert [k.toString() for k in main.act_problems.shortcuts()] == ["Ctrl+Shift+P"]
    assert main.act_problems in main.findChildren(type(main.act_problems))
    main.close()
    assert not main.problems_window.isVisible()  # closing the main window closes it too
