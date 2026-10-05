"""M3 acceptance: a keyboard-only review pass, and closing/reopening the UI never interrupts checks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.bookmark_editor import BookmarkEditor
from pagewatch.ui.windows.false_positive import FalsePositiveDialog
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.viewer import T_HIGHLIGHT, T_LOG, T_NEW, T_OLD, T_TEXT
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article

K = Qt.Key
N = 3


def make_unread(eng: EngineThread, client: ApiClient, n: int = N) -> list[int]:
    ids = []
    for i in range(n):
        eng.site.set(f"/n{i}", article(f"story {i} alpha", title=f"Paper {i}"))
        out = client.create_bookmark(
            {
                "url": eng.site.url(f"/n{i}"),
                "name": f"Paper {i}",
                "schedule": {"interval_s": 60, "jitter_pct": 0},
            }
        )
        ids.append(out.id)
    eng.settle()
    for i in range(n):
        eng.site.set(
            f"/n{i}", article(f"story {i} alpha", f"story {i} BREAKING", title=f"Paper {i}")
        )
    eng.advance(70)
    assert client.counts().unread == n
    return ids


@pytest.fixture
def open_window(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> Iterator[Callable[[], MainWindow]]:
    windows: list[MainWindow] = []

    def factory() -> MainWindow:
        client = ApiClient(eng.base, eng.engine.token)
        events = EventStream(client.ws_url, client.token)
        w = MainWindow(client, events)
        events.start()
        w.show()
        w.activateWindow()
        QTest.qWaitForWindowActive(w)
        pump_until(lambda: events.connected, what="event stream")
        windows.append(w)
        return w

    yield factory
    for w in windows:
        w.close()


def rendered(
    w: MainWindow,
    pump_until: Callable[..., None],
    action: Callable[[], None],
    tab: int = T_HIGHLIGHT,
) -> None:
    """Run ``action`` and wait for the viewer to finish rendering the given tab."""
    hits: list[tuple[int, str]] = []
    w.viewer.rendered.connect(lambda t, v: hits.append((t, v)))
    action()
    pump_until(lambda: any(t == tab for t, _ in hits), what=f"viewer tab {tab}")


def test_a_full_review_pass_is_possible_keyboard_only(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    ids = make_unread(eng, api_client)
    w = open_window()
    pump_until(lambda: w.model.rowCount() == N, what="list loaded")
    pump_until(lambda: w.tree.topLevelItem(2).text(0) == f"Unread ({N})", what="counts")

    # N: next unread -> selects it and shows the highlighted change
    rendered(w, pump_until, lambda: QTest.keyClick(w, K.Key_N))
    first = w.current_summary()
    assert first is not None and first.unread and first.id in ids
    html = w.viewer.web.last_html
    assert "<ins" in html and "BREAKING" in html and "pw-view" in html and "<script" not in html

    # Ctrl+2 / Ctrl+3 / Ctrl+4: the Text diff, New and Old tabs, all by keyboard
    rendered(
        w,
        pump_until,
        lambda: QTest.keyClick(w, K.Key_2, Qt.KeyboardModifier.ControlModifier),
        T_TEXT,
    )
    assert 'class="pw-b pw-ins"' in w.viewer.web.last_html
    rendered(
        w,
        pump_until,
        lambda: QTest.keyClick(w, K.Key_3, Qt.KeyboardModifier.ControlModifier),
        T_NEW,
    )
    assert (
        "BREAKING" in w.viewer.web.last_html
        and "<ins" not in w.viewer.web.last_html.split("<body")[1]
    )
    rendered(
        w,
        pump_until,
        lambda: QTest.keyClick(w, K.Key_4, Qt.KeyboardModifier.ControlModifier),
        T_OLD,
    )
    assert "BREAKING" not in w.viewer.web.last_html.split("<body")[1]
    QTest.keyClick(w, K.Key_6, Qt.KeyboardModifier.ControlModifier)
    pump_until(lambda: w.viewer.log.rowCount() >= 2, what="check log")  # first + changed
    assert w.viewer.current_tab() == T_LOG
    rendered(
        w,
        pump_until,
        lambda: QTest.keyClick(w, K.Key_1, Qt.KeyboardModifier.ControlModifier),
        T_HIGHLIGHT,
    )

    # O: open the page in the default browser (intercepted)
    opened: list[str] = []
    monkeypatch.setattr(w, "_open_external", opened.append)
    QTest.keyClick(w, K.Key_O)
    assert opened == [first.url]

    # R: mark read -> the row is no longer unread, in the engine as well
    QTest.keyClick(w, K.Key_R)
    pump_until(lambda: not w.model.summary(w.table.currentIndex().row()).unread, what="marked read")
    assert not api_client.bookmark(first.id).unread
    assert api_client.counts().unread == N - 1

    # Space: next unread again (the second shortcut for the same action)
    rendered(w, pump_until, lambda: QTest.keyClick(w, K.Key_Space))
    second = w.current_summary()
    assert second is not None and second.unread and second.id != first.id

    # C: check the selection now (a request reaches the fixture site)
    hits = len(eng.site.hits_for(f"/n{ids.index(second.id)}"))
    QTest.keyClick(w, K.Key_C)
    pump_until(
        lambda: len(eng.site.hits_for(f"/n{ids.index(second.id)}")) > hits, what="manual check"
    )

    # F: flag as false positive opens the proposal dialog; Ctrl+E opens the editor
    seen: list[Any] = []
    monkeypatch.setattr(w, "_exec", lambda dialog: (seen.append(dialog), False)[1])
    QTest.keyClick(w, K.Key_F)
    pump_until(
        lambda: any(isinstance(d, FalsePositiveDialog) for d in seen), what="false-positive dialog"
    )
    QTest.keyClick(w, K.Key_E, Qt.KeyboardModifier.ControlModifier)
    pump_until(lambda: any(isinstance(d, BookmarkEditor) for d in seen), what="editor")

    # R, N, R: read everything that is left, then N reports there is nothing more
    QTest.keyClick(w, K.Key_R)
    pump_until(lambda: api_client.counts().unread == 1, what="second read")
    rendered(w, pump_until, lambda: QTest.keyClick(w, K.Key_N))
    QTest.keyClick(w, K.Key_R)
    pump_until(lambda: api_client.counts().unread == 0, what="all read")
    pump_until(
        lambda: not any(w.model.summary(r).unread for r in range(w.model.rowCount())),
        what="list updated",
    )
    QTest.keyClick(w, K.Key_N)
    pump_until(lambda: w.lbl_message.text() == "No more unread changes", what="end of review")


def test_next_unread_pages_through_the_lazy_list(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    make_unread(eng, api_client, 2)
    # 130 read bookmarks sort before the unread ones, so N must fetch more pages to find them
    for i in range(130):
        eng.site.set(f"/r{i}", article(f"read {i}"))
        api_client.create_bookmark(
            {
                "url": eng.site.url(f"/r{i}"),
                "name": f"A read {i:03d}",
                "schedule": {"mode": "manual"},
            }
        )
    eng.settle()
    w = open_window()
    w.model._page_size = 50
    w.search.setText("")
    w.model.sort(1, Qt.SortOrder.AscendingOrder)  # by name: "A read ..." first, "Paper ..." last
    pump_until(lambda: w.model.rowCount() == 50 and w.model.total == 132, what="first page")
    assert w.model.canFetchMore()
    QTest.keyClick(w, K.Key_N)
    pump_until(
        lambda: (c := w.current_summary()) is not None and c.unread and c.name.startswith("Paper"),
        what="paged search",
    )
    assert w.model.rowCount() >= 130


def test_toolbar_autowatch_and_status_bar_follow_the_engine(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    w = open_window()
    pump_until(lambda: w.lbl_conn.text() == "engine: connected", what="health")
    assert w.act_autowatch.text() == "Pause AutoWatch"
    QTest.keyClick(w, K.Key_P, Qt.KeyboardModifier.ControlModifier)
    pump_until(lambda: "AutoWatch paused" in w.lbl_state.text(), what="paused")
    assert (
        w.act_autowatch.text() == "Start AutoWatch"
        and api_client.health().autowatch.state == "paused"
    )
    QTest.keyClick(w, K.Key_P, Qt.KeyboardModifier.ControlModifier)
    pump_until(lambda: "paused" not in w.lbl_state.text(), what="resumed")


def test_closing_and_reopening_the_ui_never_interrupts_checks(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    ids = make_unread(eng, api_client)
    w = open_window()
    pump_until(lambda: w.model.rowCount() == N)
    assert eng.engine.events.subscribers >= 1  # the UI is listening

    def checks() -> int:
        return len(eng.site.hits)

    before = checks()
    eng.advance(130)  # time passes while the window is open: checks continue
    open_checks = checks() - before
    assert open_checks >= N

    w.close()
    pump_until(lambda: eng.engine.events.subscribers == 0, what="UI unsubscribed")
    before = checks()
    eng.site.set(
        "/n0",
        article(
            "story 0 alpha", "story 0 BREAKING", "story 0 and then some more news", title="Paper 0"
        ),
    )
    eng.advance(130)  # no UI at all: the engine alone keeps checking and detecting
    assert checks() - before >= N
    assert (
        api_client.bookmark(ids[0]).latest_version_id
        != api_client.bookmark(ids[0]).baseline_version_id
    )
    assert (
        len(api_client.changes(ids[0]).items) == 2
    )  # the change made while the UI was closed was found

    w2 = open_window()  # reopen: it shows the up-to-date state, and checks go on
    pump_until(lambda: w2.model.rowCount() == N, what="reopened list")
    w2.model.set_query(unread=True)
    pump_until(lambda: w2.model.total == N, what="unread after reopen")
    before = checks()
    eng.advance(130)
    assert checks() - before >= N
    assert api_client.health().in_flight == 0  # nothing was left stuck
