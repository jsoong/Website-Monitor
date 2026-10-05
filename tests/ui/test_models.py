from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QModelIndex, Qt

from pagewatch.models import BookmarkCounts
from pagewatch.ui.client import ApiClient
from pagewatch.ui.models import (
    BUILTINS,
    COL_NAME,
    BookmarkListModel,
    FolderTree,
    SummaryRole,
    builtin_query,
    fmt_interval,
    fmt_time,
)
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article


def make_bookmarks(eng: EngineThread, client: ApiClient, n: int, **kw: Any) -> list[int]:
    ids = []
    for i in range(n):
        eng.site.set(f"/p{i}", article(f"item {i}", title=f"P{i}"))
        out = client.create_bookmark(
            {
                "url": eng.site.url(f"/p{i}"),
                "name": f"item {i:02d}",
                "schedule": {"mode": "manual"},
                **kw,
            }
        )
        ids.append(out.id)
    eng.settle()
    return ids


def test_list_model_loads_lazily_page_by_page(
    eng: EngineThread, api_client: ApiClient, pump_until: Callable[..., None]
) -> None:
    make_bookmarks(eng, api_client, 25)
    model = BookmarkListModel(api_client, page_size=10)
    model.set_query()
    pump_until(lambda: model.rowCount() == 10, what="first page")
    assert model.total == 25 and model.canFetchMore(QModelIndex())
    model.fetchMore(QModelIndex())
    pump_until(lambda: model.rowCount() == 20, what="second page")
    model.fetchMore(QModelIndex())
    pump_until(lambda: model.rowCount() == 25, what="third page")
    assert not model.canFetchMore(QModelIndex())
    names = [model.summary(r).name for r in range(25)]
    assert (
        names == [f"item {i:02d}" for i in range(25)] and len(set(names)) == 25
    )  # no duplicates across pages


def test_sorting_and_filtering_are_done_by_the_engine(
    eng: EngineThread, api_client: ApiClient, pump_until: Callable[..., None]
) -> None:
    make_bookmarks(eng, api_client, 25)
    model = BookmarkListModel(api_client, page_size=10)
    model.set_query()
    pump_until(lambda: model.rowCount() == 10)
    model.sort(COL_NAME, Qt.SortOrder.DescendingOrder)
    pump_until(
        lambda: model.rowCount() == 10 and model.summary(0).name == "item 24", what="sorted reload"
    )
    assert [model.summary(r).name for r in range(3)] == ["item 24", "item 23", "item 22"]
    model.sort(5, Qt.SortOrder.AscendingOrder)  # "Every" is not sortable by the engine: ignored
    assert model.summary(0).name == "item 24"
    model.set_query(q="item 07")
    pump_until(lambda: model.rowCount() == 1 and model.total == 1, what="filtered")
    assert model.summary(0).name == "item 07"
    model.set_query(unread=True)
    pump_until(lambda: model.total == 0 and not model.loading, what="unread filter")
    assert model.rowCount() == 0


def test_a_stale_answer_is_dropped_when_the_query_changes_meanwhile(
    eng: EngineThread, api_client: ApiClient, pump_until: Callable[..., None]
) -> None:
    make_bookmarks(eng, api_client, 12)
    model = BookmarkListModel(api_client, page_size=5)
    model.set_query()
    model.set_query(q="item 03")  # the first request is still in flight
    model.set_query(q="item 11")
    pump_until(lambda: model.total == 1 and not model.loading, what="last query wins")
    assert [model.summary(r).name for r in range(model.rowCount())] == ["item 11"]


def test_roles_unread_bold_status_icon_and_tooltip_and_single_row_refresh(
    eng: EngineThread, api_client: ApiClient, pump_until: Callable[..., None]
) -> None:
    (bid, *_) = make_bookmarks(eng, api_client, 2, schedule={"interval_s": 60, "jitter_pct": 0})
    eng.site.set("/p0", article("item 0", "something new", title="P0"))
    eng.advance(70)
    model = BookmarkListModel(api_client)
    model.set_query()
    pump_until(lambda: model.rowCount() == 2)
    changed = model.index(0, COL_NAME)
    assert model.data(changed, Qt.ItemDataRole.FontRole).bold()
    assert model.data(model.index(1, COL_NAME), Qt.ItemDataRole.FontRole) is None
    assert not model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole).isNull()
    assert api_client_url(eng, 0) in model.data(changed, Qt.ItemDataRole.ToolTipRole)
    assert model.data(changed, SummaryRole).id == bid
    assert model.data(model.index(0, 5), Qt.ItemDataRole.DisplayRole) == "1m"

    api_client.patch_bookmark(bid, {"name": "Renamed"})
    model.refresh_bookmark(bid)
    pump_until(lambda: model.summary(0).name == "Renamed", what="row refresh")
    api_client.mark_read(bid)
    model.refresh_bookmark(bid)
    pump_until(lambda: not model.summary(0).unread)
    assert model.data(model.index(0, COL_NAME), Qt.ItemDataRole.FontRole) is None


def api_client_url(eng: EngineThread, i: int) -> str:
    return eng.site.url(f"/p{i}")


def test_folder_tree_builtins_folders_counts_and_queries(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    api_client._call("POST", "/folders", json={"name": "News"})
    parent = api_client.folders()[0]
    api_client._call("POST", "/folders", json={"name": "Local", "parent_id": parent.id})
    make_bookmarks(eng, api_client, 3, folder_id=parent.id)
    tree = FolderTree()
    selected: list[dict[str, Any]] = []
    tree.query_selected.connect(selected.append)
    tree.load(api_client.folders(), api_client.counts())
    labels = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert labels[:6] == [f"{label} ({n})" if key != "all" else f"{label} ({n})"
                          for (key, label), n in zip(BUILTINS, [3, 0, 0, 0, 0, 0], strict=True)]  # fmt: skip
    folders_root = tree.topLevelItem(tree.topLevelItemCount() - 1)
    assert folders_root.text(0) == "Folders" and folders_root.child(0).text(0) == "News"
    assert folders_root.child(0).child(0).text(0) == "Local"
    tree.setCurrentItem(folders_root.child(0))
    assert selected[-1] == {"folder": parent.id} and tree.selected_folder_id() == parent.id
    tree.setCurrentItem(tree.topLevelItem(2))
    assert selected[-1] == {"unread": True}
    assert "changed_since" in builtin_query("changed_today") and builtin_query("errors") == {
        "status": "error"
    }


def test_formatting_helpers() -> None:
    assert fmt_interval(60) == "1m" and fmt_interval(3600) == "1h" and fmt_interval(90) == "90s"
    assert fmt_interval(86400) == "1d" and fmt_interval(None) == ""
    assert fmt_time(None) == "" and len(fmt_time("2026-01-05T12:00:00.000000Z")) > 5
    c = BookmarkCounts(
        total=1, unread=0, errors=0, needs_login=0, changed_today=0, keyword_hits=0, by_folder={}
    )
    assert c.total == 1
