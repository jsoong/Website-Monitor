"""M7 in the UI: saved searches in the folder tree, the saved-search dialog, import / export, and the
editor's plugin, Follow-Links and merge-pages fields."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QDialog

from pagewatch.models import BookmarkOut, PluginInfo
from pagewatch.ui.client import ApiClient
from pagewatch.ui.models import FolderTree
from pagewatch.ui.windows.bookmark_editor import BookmarkEditor
from pagewatch.ui.windows.import_export import ImportExportDialog, result_text
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.saved_search import SavedSearchDialog
from tests.support.docs import make_xlsx
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article
from tests.ui.test_main_window import open_window  # noqa: F401  (fixture)
from tests.unit.test_portability import CHROME

SCHED = {"interval_s": 60, "jitter_pct": 0}


def add(eng: EngineThread, client: ApiClient, name: str, **kw: Any) -> BookmarkOut:
    path = "/" + name.lower().replace(" ", "-")
    eng.site.set(path, article(name, title=name))
    return client.create_bookmark(
        {"url": eng.site.url(path), "name": name, "schedule": SCHED, **kw}
    )


# -- saved searches -----------------------------------------------------------------------------


def test_saved_searches_appear_in_the_tree_with_counts_and_select_like_folders(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    tree = FolderTree()
    tree.load(api_client.folders(), api_client.counts())
    assert tree._saved_root.isHidden()  # nothing to show until a saved search exists
    add(eng, api_client, "Daily news")
    add(eng, api_client, "Shop")
    eng.settle()
    saved = api_client.create_folder({"name": "News", "is_virtual": True, "query": {"q": "news"}})
    real = api_client.create_folder({"name": "Plain"})
    selected: list[dict[str, Any]] = []
    tree.query_selected.connect(selected.append)
    tree.load(api_client.folders(), api_client.counts())
    assert not tree._saved_root.isHidden() and tree._saved_root.childCount() == 1
    item = tree._saved_root.child(0)
    assert item.text(0) == "News"  # nothing unread: no count shown
    tree.setCurrentItem(item)
    assert selected[-1] == {"folder": saved.id}  # the engine resolves the search
    assert tree.selected_saved_id() == saved.id and tree.selected_folder_id() is None
    assert tree.saved_ids() == [saved.id] and real.id in tree.folder_ids()
    assert saved.id not in tree.folder_ids()  # a search is never a place to put a bookmark
    assert [b.name for b in api_client.bookmarks(folder=saved.id).items] == ["Daily news"]


def test_the_main_window_lists_a_saved_search_and_creates_edits_and_deletes_one(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    add(eng, api_client, "Daily news")
    add(eng, api_client, "Shop")
    add(eng, api_client, "Weekly news")
    eng.settle()
    w = open_window()
    pump_until(lambda: w.model.rowCount() == 3, what="list loaded")

    # "New saved search" starts from what the list is showing now
    w.search.setText("news")
    pump_until(lambda: w.model.rowCount() == 2, what="search applied")
    seen: list[SavedSearchDialog] = []

    def fill(dialog: QDialog) -> bool:
        assert isinstance(dialog, SavedSearchDialog)
        seen.append(dialog)
        assert dialog.text.text() == "news"  # prefilled from the search box
        dialog.name_edit.setText("All the news")
        return True

    monkeypatch.setattr(w, "_exec", fill)
    w.new_saved_search()
    pump_until(lambda: len(api_client.folders()) == 1, what="folder created")
    (folder,) = api_client.folders()
    assert folder.is_virtual and folder.name == "All the news" and folder.query == {"q": "news"}
    pump_until(lambda: w.tree.saved_ids() == [folder.id], what="tree refreshed")
    w.search.setText("")
    w.tree.setCurrentItem(w.tree._saved_items[folder.id])
    pump_until(lambda: w.model.rowCount() == 2 and w.model.total == 2, what="saved search listed")
    assert sorted(w.model.summary(r).name for r in range(2)) == ["Daily news", "Weekly news"]

    # edit: narrow it to "weekly"
    def narrow(dialog: QDialog) -> bool:
        assert isinstance(dialog, SavedSearchDialog) and dialog.name() == "All the news"
        dialog.text.setText("weekly")
        dialog.name_edit.setText("Weekly")
        return True

    monkeypatch.setattr(w, "_exec", narrow)
    w.edit_saved_search(folder.id)
    pump_until(lambda: api_client.folders()[0].name == "Weekly", what="renamed")
    assert api_client.folders()[0].query == {"q": "weekly"}
    pump_until(lambda: w.model.total == 1, what="list reloaded")

    # delete it: the bookmarks stay
    w.client.delete_folder(folder.id)
    w._refresh_side()
    pump_until(lambda: not w.tree.saved_ids(), what="tree refreshed")
    assert api_client.counts().total == 3


def test_the_saved_search_dialog_collects_only_what_was_set_and_needs_a_name_and_a_criterion(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:  # fmt: skip
    folder = api_client.create_folder({"name": "Plain"})
    d = SavedSearchDialog(api_client.folders())
    d._save()
    assert "name" in d.error.text() and d.result() == QDialog.DialogCode.Rejected
    d.name_edit.setText("Mine")
    d._save()
    assert "criterion" in d.error.text()
    d.status.setCurrentIndex(d.status.findData("error"))
    d.unread.setCurrentIndex(d.unread.findData(True))
    d.folder.setCurrentIndex(d.folder.findData(folder.id))
    d.within.setValue(6)
    d.errors.setValue(2)
    d.hits.setChecked(True)
    d.source.setCurrentIndex(d.source.findData("feed"))
    d.method.setCurrentIndex(d.method.findData("browser"))
    d.plugin.setText("price_range")
    d.enabled.setCurrentIndex(d.enabled.findData(False))
    d.text.setText(" shop ")
    assert d.query() == {
        "q": "shop", "status": "error", "unread": True, "enabled": False, "folder": folder.id,
        "source_type": "feed", "check_method": "browser", "keyword_hits": True,
        "changed_within_h": 6.0, "plugin": "price_range", "min_errors": 2,
    }  # fmt: skip
    d._save()
    assert d.result() == QDialog.DialogCode.Accepted and d.error.text() == ""
    # the engine accepts exactly that
    made = api_client.create_folder({"name": d.name(), "is_virtual": True, "query": d.query()})
    again = SavedSearchDialog(api_client.folders(), name=made.name, query=made.query)
    assert again.query() == d.query()


# -- import / export ----------------------------------------------------------------------------


def test_import_a_file_with_a_dry_run_first_then_for_real(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    tmp_path: Path,
) -> None:  # fmt: skip
    marks = tmp_path / "bookmarks.html"
    marks.write_text(CHROME, encoding="utf-8")
    d = ImportExportDialog(api_client, api_client.folders())
    changed: list[bool] = []
    d.changed.connect(lambda: changed.append(True))
    d.start_import()  # no file chosen
    assert "Choose a file" in d.report.toPlainText()
    d.start_import(str(marks), dry_run=True)
    pump_until(lambda: d.report.toPlainText().startswith("Would import 4"), what="dry run")
    assert "2 folder(s) would be created" in d.report.toPlainText() and not changed
    assert api_client.counts().total == 0
    d.import_path_edit.setText(str(marks))
    d.start_import()
    pump_until(lambda: d.report.toPlainText().startswith("Imported 4"), what="import")
    assert changed == [True] and api_client.counts().total == 4 and d.import_button.isEnabled()
    d.start_import(str(marks))  # again: everything is a duplicate
    pump_until(lambda: "4 already existed" in d.report.toPlainText(), what="duplicates")
    assert changed == [True]  # nothing new, so no reload
    d.deleteLater()


def test_import_reads_csv_xlsx_and_reports_bad_rows_and_unreadable_files(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    tmp_path: Path,
) -> None:  # fmt: skip
    folder = api_client.create_folder({"name": "Inbox"})
    d = ImportExportDialog(api_client, api_client.folders(), folder_id=folder.id)
    assert d.into.currentData() == folder.id  # the selected folder is the default target
    csv_file = tmp_path / "list.csv"
    csv_file.write_text(
        "name,url,interval\nGood,https://good.example.com,30m\nBad,ftp:/x,\nSlow,https://s.test,5\n",
        encoding="utf-8",
    )
    d.start_import(str(csv_file))
    pump_until(lambda: d.report.toPlainText().startswith("Imported 1"), what="csv")
    assert "row 3:" in d.report.toPlainText() and "row 4:" in d.report.toPlainText()
    xlsx = tmp_path / "list.xlsx"
    xlsx.write_bytes(make_xlsx({"S": [["name", "url"], ["Sheet", "https://sheet.example.com"]]}))
    d.start_import(str(xlsx))
    pump_until(lambda: "from xlsx" in d.report.toPlainText(), what="xlsx")
    assert {b.name for b in api_client.bookmarks().items} == {"Good", "Sheet"}
    assert all(b.folder_id == folder.id for b in api_client.bookmarks().items)
    d.start_import(str(tmp_path / "missing.csv"))
    pump_until(lambda: d.report.toPlainText().startswith("Import failed"), what="missing file")
    assert d.import_button.isEnabled()
    d.deleteLater()


def test_export_writes_each_format_for_everything_a_folder_or_the_selection(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None],
    tmp_path: Path,
) -> None:  # fmt: skip
    folder = api_client.create_folder({"name": "Feeds"})
    feed = add(eng, api_client, "A feed", folder_id=folder.id, source_type="feed")
    other = add(eng, api_client, "Other page")
    d = ImportExportDialog(
        api_client, api_client.folders(), selected_ids=[other.id], folder_id=folder.id
    )
    assert [d.scope.itemText(i) for i in range(d.scope.count())] == [
        "Everything", "The selected folder", "The 1 selected bookmark(s)",
    ]  # fmt: skip

    def export(fmt: str, scope: int) -> str:
        d.format.setCurrentIndex(d.format.findData(fmt))
        d.scope.setCurrentIndex(scope)
        out = tmp_path / f"out-{fmt}-{scope}"
        d.export_status.setText("")
        d.start_export(str(out))
        pump_until(lambda: "Saved" in d.export_status.text(), what="export")
        return out.read_text(encoding="utf-8-sig")

    assert export("json", 0).count('"url"') == 2
    assert export("json", 1).count('"url"') == 1 and "A feed" in export("json", 1)
    assert export("json", 2).count('"url"') == 1 and "Other page" in export("json", 2)
    assert "xmlUrl" in export("opml", 0) and "Other page" not in export("opml", 0)
    assert export("csv", 0).splitlines()[0].startswith("id,name,url")
    assert feed.id and d.export_button.isEnabled()
    d.start_export(str(tmp_path / "no" / "such" / "dir" / "x.json"))
    pump_until(lambda: "Export failed" in d.export_status.text(), what="failure")
    assert d.export_button.isEnabled()
    d.deleteLater()


def test_result_text_lists_the_first_problems_only() -> None:
    from pagewatch.models import ImportResult, ImportRowError

    res = ImportResult(
        format="csv", dry_run=False, created=2, skipped_duplicates=1, folders_created=0,
        errors=[ImportRowError(row=n, message="bad") for n in range(60)],
    )  # fmt: skip
    text = result_text(res)
    assert text.startswith("Imported 2 bookmark(s) from csv; 1 already existed")
    assert text.count("row ") == 50 and "and 10 more problems" in text


def test_the_main_window_opens_import_export_with_its_selection(
    eng: EngineThread, api_client: ApiClient, open_window: Callable[[], MainWindow],  # noqa: F811
    pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:  # fmt: skip
    add(eng, api_client, "One")
    eng.settle()
    w = open_window()
    pump_until(lambda: w.model.rowCount() == 1, what="list")
    w.table.selectRow(0)
    opened: list[ImportExportDialog] = []

    def fake(dialog: QDialog) -> bool:
        assert isinstance(dialog, ImportExportDialog)
        opened.append(dialog)
        marks = tmp_path / "m.html"
        marks.write_text(CHROME, encoding="utf-8")
        dialog.start_import(str(marks))
        pump_until(lambda: dialog.report.toPlainText().startswith("Imported"), what="import")
        return False

    monkeypatch.setattr(w, "_exec", fake)
    w.import_export()
    assert opened and "The 1 selected bookmark(s)" in [
        opened[0].scope.itemText(i) for i in range(opened[0].scope.count())
    ]
    pump_until(lambda: w.model.total == 5, what="the list reloaded after the import")
    assert w.act_import.shortcut().toString() == "Ctrl+Shift+I"
    assert [a.text() for a in w.menuBar().actions()] == ["&File", "&Tools"]


# -- the editor ---------------------------------------------------------------------------------


def editor(client: ApiClient, bid: int, plugins: list[PluginInfo] | None = None) -> BookmarkEditor:
    return BookmarkEditor(client, [client.bookmark(bid)], client.folders(), plugins=plugins)


def test_the_editor_assigns_a_plugin_and_changes_nothing_when_untouched(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    (eng.engine.data_dir.plugins_dir / "mine.py").write_text(
        "def compare(ctx, old, new): return None\n", encoding="utf-8"
    )
    b = add(eng, api_client, "Plugged")
    eng.settle()
    plugins = [p for p in api_client.plugins() if p.installed]
    assert [p.name for p in plugins] == ["mine"]
    ed = editor(api_client, b.id, plugins)
    assert [ed.plugin.itemText(i) for i in range(ed.plugin.count())] == ["(none)", "mine"]
    assert ed.build_patch() == {}  # opening and saving an untouched bookmark sends nothing
    ed.plugin.setCurrentIndex(ed.plugin.findData("mine"))
    assert ed.build_patch() == {"plugin": "mine"}
    api_client.patch_bookmark(b.id, {"plugin": "mine"})
    again = editor(api_client, b.id, plugins)
    assert again.plugin.currentData() == "mine" and again.build_patch() == {}
    again.plugin.setCurrentIndex(0)
    assert again.build_patch() == {"plugin": None}
    gone = editor(api_client, b.id, [])  # a plugin that is no longer listed stays selectable
    assert gone.plugin.currentData() == "mine"
    ed.deleteLater()


def test_follow_links_and_merge_pages_round_trip_through_the_editor(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    b = add(eng, api_client, "Hub")
    ed = editor(api_client, b.id)
    assert ed.build_patch() == {} and not ed.follow_on.isChecked()
    assert ed.merge_mode.currentData() is None

    ed.follow_on.setChecked(True)
    ed.follow_include.setPlainText("/news/*\n/blog/*")
    ed.follow_exclude.setPlainText("*.pdf")
    ed.follow_depth.setValue(2)
    ed.follow_pages.setValue(20)
    ed.follow_same.setChecked(False)
    ed.merge_mode.setCurrentIndex(ed.merge_mode.findData("selector"))
    ed.merge_value.setText("a.next")
    ed.merge_pages.setValue(7)
    patch = ed.build_patch()
    assert patch["fetch"]["follow"] == {
        "enabled": True, "include": ["/news/*", "/blog/*"], "exclude": ["*.pdf"],
        "pattern_kind": "wildcard", "depth": 2, "max_pages": 20, "same_host": False,
        "folder_name": None,
    }  # fmt: skip
    assert (
        patch["fetch"]["merge"]["next_selector"] == "a.next"
        and patch["fetch"]["merge"]["max_pages"] == 7
    )
    saved = api_client.patch_bookmark(b.id, patch)
    assert saved.fetch.follow is not None and saved.fetch.follow.depth == 2
    assert saved.fetch.merge is not None and saved.fetch.merge.next_selector == "a.next"

    reopened = editor(api_client, b.id)
    assert reopened.follow_on.isChecked() and reopened.follow_depth.value() == 2
    assert reopened.follow_include.toPlainText() == "/news/*\n/blog/*"
    assert (
        reopened.merge_mode.currentData() == "selector" and reopened.merge_value.text() == "a.next"
    )
    assert reopened.build_patch() == {}  # nothing invented on the way back
    reopened.follow_depth.setValue(3)
    assert reopened.build_patch() == {"fetch": {"follow": {"depth": 3}}}  # only what changed
    reopened.follow_depth.setValue(2)

    # switch them off: disabled, not forgotten
    reopened.follow_on.setChecked(False)
    reopened.merge_mode.setCurrentIndex(0)
    off = api_client.patch_bookmark(b.id, reopened.build_patch())
    assert off.fetch.follow is not None and off.fetch.follow.enabled is False
    assert off.fetch.merge is not None and off.fetch.merge.enabled is False
    assert off.fetch.follow.include == ["/news/*", "/blog/*"]
    again = editor(api_client, b.id)
    assert not again.follow_on.isChecked() and again.merge_mode.currentData() is None
    assert again.build_patch() == {}

    # a URL pattern
    again.merge_mode.setCurrentIndex(again.merge_mode.findData("pattern"))
    again.merge_value.setText("/list?page={n}")
    patch = again.build_patch()
    assert patch["fetch"]["merge"]["url_pattern"] == "/list?page={n}"
    assert (
        patch["fetch"]["merge"]["enabled"] is True
        and patch["fetch"]["merge"]["next_selector"] is None
    )


def test_the_editor_explains_bad_follow_and_merge_settings_instead_of_saving(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    b = add(eng, api_client, "Hub")
    ed = editor(api_client, b.id)
    ed.merge_mode.setCurrentIndex(ed.merge_mode.findData("pattern"))
    ed.merge_value.setText("/list?page=2")  # no {n}
    ed.save()
    assert "{n}" in ed.error.text()
    ed.merge_mode.setCurrentIndex(ed.merge_mode.findData("selector"))
    ed.merge_value.setText("a[[")
    ed.save()
    assert "Merge pages" in ed.error.text() and "selector" in ed.error.text()
    ed.merge_mode.setCurrentIndex(0)
    ed.follow_on.setChecked(True)
    ed.follow_kind.setCurrentIndex(ed.follow_kind.findData("regex"))
    ed.follow_include.setPlainText("(")
    ed.save()
    assert "Follow links" in ed.error.text() and "regex" in ed.error.text()
    assert api_client.bookmark(b.id).fetch.follow is None  # nothing was saved
