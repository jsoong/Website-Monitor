"""M3 acceptance: the bookmark list stays responsive with 10,000 bookmarks (spec: scroll, sort
and filter respond in under 200 ms)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableView

from pagewatch.engine.clock import iso
from pagewatch.ui.client import ApiClient
from pagewatch.ui.models import COL_NAME, BookmarkListModel
from tests.support.engine_thread import EngineThread

N = 10_000
BUDGET_S = 0.2


def populate(eng: EngineThread, n: int) -> None:
    now = iso(eng.clock.now())

    def write(conn: sqlite3.Connection) -> None:
        rows = []
        for i in range(n):
            status = "error" if i % 50 == 0 else ("changed" if i % 7 == 0 else "ok")
            rows.append(
                (
                    f"item {i:05d}", f"https://host{i % 400}.example/page/{i}", "{}", status,
                    int(i % 7 == 0), now if i % 7 == 0 else None, now, now,
                )
            )  # fmt: skip
        conn.executemany(
            "INSERT INTO bookmark(name, url, schedule_json, status, unread, last_changed_at, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )

    eng.engine.db.write_sync(write)


def test_ten_thousand_bookmarks_scroll_sort_and_filter_in_under_200ms(
    eng: EngineThread, api_client: ApiClient, qapp: QApplication, pump_until: Callable[..., None]
) -> None:
    populate(eng, N)
    model = BookmarkListModel(api_client)  # the production page size
    view = QTableView()
    view.setModel(model)
    view.resize(900, 600)
    view.show()
    loaded: list[int] = []
    model.page_loaded.connect(lambda: loaded.append(model.rowCount()))

    def measure(
        action: Callable[[], None], what: str, settle: Callable[[], bool] | None = None
    ) -> float:
        n_before = len(loaded)
        t0 = time.perf_counter()
        action()
        pump_until(lambda: len(loaded) > n_before and (settle is None or settle()), what=what)
        return time.perf_counter() - t0

    timings: dict[str, float] = {}
    timings["first page"] = measure(model.set_query, "first page")
    assert model.total == N and model.rowCount() == 200

    # scrolling: every further page, all the way to the end
    worst_scroll = 0.0
    while model.canFetchMore():
        worst_scroll = max(worst_scroll, measure(model.fetchMore, "next page"))
    timings["worst scroll page"] = worst_scroll
    assert model.rowCount() == N and len({model.summary(r).id for r in range(N)}) == N

    # reading every loaded row (what painting 10,000 rows costs the model)
    t0 = time.perf_counter()
    for r in range(0, N, 1):
        model.data(model.index(r, COL_NAME), Qt.ItemDataRole.DisplayRole)
    timings["10k data() calls"] = time.perf_counter() - t0
    view.scrollToBottom()
    QApplication.processEvents()

    timings["sort by name desc"] = measure(
        lambda: model.sort(COL_NAME, Qt.SortOrder.DescendingOrder),
        "sort",
        lambda: model.rowCount() == 200,
    )
    assert model.summary(0).name == "item 09999"
    timings["sort by last changed"] = measure(
        lambda: model.sort(2, Qt.SortOrder.DescendingOrder),
        "sort2",
        lambda: model.rowCount() == 200,
    )
    timings["filter by text"] = measure(
        lambda: model.set_query(q="item 0770"), "filter", lambda: model.total == 10
    )
    assert model.rowCount() == 10 and model.summary(0).name.startswith("item 0770")
    timings["filter unread"] = measure(
        lambda: model.set_query(unread=True), "unread", lambda: model.total == 1429
    )
    timings["filter errors"] = measure(
        lambda: model.set_query(status="error"), "errors", lambda: model.total == 200
    )

    print("10k timings (ms):", {k: round(v * 1000) for k, v in timings.items()})  # visible with -s
    slow = {k: round(v * 1000) for k, v in timings.items() if v >= BUDGET_S}
    assert not slow, (
        f"over the {BUDGET_S * 1000:.0f} ms budget (ms): {slow}; all: { {k: round(v * 1000) for k, v in timings.items()} }"
    )
    view.close()
