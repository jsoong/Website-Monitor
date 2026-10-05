from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from pagewatch.models import FalsePositiveOut, FilterRule, ProposalOut
from pagewatch.ui.client import ApiClient
from pagewatch.ui.windows.add_assistant import AddBookmarkDialog
from pagewatch.ui.windows.bookmark_editor import BookmarkEditor, best_unit, diff_dict
from pagewatch.ui.windows.false_positive import FalsePositiveDialog
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article


def make(eng: EngineThread, client: ApiClient, name: str = "Shop", **kw: Any) -> int:
    path = "/" + name.lower().replace(" ", "-")
    eng.site.set(path, article("Tea $4", "Coffee $5", title=name))
    out = client.create_bookmark({"url": eng.site.url(path), "name": name,
                                  "schedule": {"interval_s": 3600, "jitter_pct": 0}, **kw})  # fmt: skip
    eng.settle()
    return out.id


def test_diff_dict_and_unit_helpers() -> None:
    assert diff_dict({"a": 1, "n": {"x": 1, "y": 2}}, {"a": 1, "n": {"x": 1, "y": 3}}) == {
        "n": {"y": 3}
    }
    assert diff_dict({"w": {"start": "a"}}, {"w": None}) == {"w": None}  # None removes the override
    assert diff_dict({"l": [1]}, {"l": [1, 2]}) == {"l": [1, 2]}
    assert (
        best_unit(3600) == (1, 3600)
        and best_unit(5400) == (90, 60)
        and best_unit(172800) == (2, 86400)
    )


def test_editor_sends_only_what_changed(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = make(eng, api_client)
    out = api_client.bookmark(bid)
    ed = BookmarkEditor(api_client, [out], api_client.folders())
    assert ed.build_patch() == {}  # opening and saving an untouched bookmark changes nothing
    assert ed.interval.value() == 1 and ed.unit.currentText() == "hours"

    ed.interval.setValue(2)
    ed.min_words.setValue(5)
    ed.keywords.setPlainText('"tea" + "$4"')
    ed.specials["ignore_case"].setChecked(False)
    ed.add_rule("ignore", None)
    ed._set(0, 1, "text")
    ed._set(0, 2, "wildcard")
    ed._set(0, 3, "Updated * ago")
    ed.name.setText("Shop 2")
    ed.hotsite.setChecked(True)
    patch = ed.build_patch()
    assert patch == {
        "name": "Shop 2",
        "priority": 1,
        "schedule": {"interval_s": 7200},
        "filter": {
            "ignore": [{"type": "text", "pattern": "Updated * ago", "pattern_kind": "wildcard"}],
            "special": {"ignore_case": False},
        },
        "gate": {"min_changed_words": 5, "keywords": '"tea" + "$4"'},
    }
    ed.save()
    pump_until(lambda: api_client.bookmark(bid).name == "Shop 2", what="saved")
    after = api_client.bookmark(bid)
    assert (
        after.schedule.interval_s == 7200
        and after.gate.min_changed_words == 5
        and after.priority == 1
    )
    assert after.overrides["schedule"] == {"interval_s": 7200, "jitter_pct": 0}  # only what was set
    assert (
        after.filter.ignore[0].pattern == "Updated * ago" and not after.filter.special.ignore_case
    )


def test_editor_shows_engine_validation_errors_and_stays_open(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = make(eng, api_client)
    ed = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    ed.keywords.setPlainText("regex(")
    ed.save()
    pump_until(
        lambda: "parenthes" in ed.error.text().lower() or "regular" in ed.error.text().lower(),
        what="engine error",
    )
    assert (
        ed.result() != QDialog.DialogCode.Accepted and api_client.bookmark(bid).gate.keywords == ""
    )
    # an invalid filter rule never reaches the engine
    ed.keywords.setPlainText("")
    ed.add_rule("ignore")
    ed._set(0, 1, "selector")
    ed._set(0, 3, "p[")
    ed.save()
    assert "Invalid filter rule" in ed.error.text()


def test_bulk_edit_applies_only_the_ticked_tabs_to_every_bookmark(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    a, b = make(eng, api_client, "Alpha"), make(eng, api_client, "Beta")
    outs = [api_client.bookmark(a), api_client.bookmark(b)]
    ed = BookmarkEditor(api_client, outs, api_client.folders())
    assert ed.bulk and ed.build_patch() == {}  # nothing ticked: nothing to send
    ed.min_chars.setValue(120)
    ed.interval.setValue(5)  # changed but its tab is not ticked
    ed._apply["Gate"].setChecked(True)
    patch = ed.build_patch()
    assert set(patch) == {"gate"} and patch["gate"]["min_chars"] == 120 and "name" not in patch
    ed.save()
    pump_until(
        lambda: all(api_client.bookmark(i).gate.min_chars == 120 for i in (a, b)), what="bulk saved"
    )
    assert all(api_client.bookmark(i).schedule.interval_s == 3600 for i in (a, b))


def test_editor_test_filter_previews_against_the_stored_pages(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = make(eng, api_client, schedule={"interval_s": 60, "jitter_pct": 0})
    eng.site.set("/shop", article("Tea $3", "Coffee $5", title="Shop"))
    eng.advance(70)
    ed = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    ed.run_test_filter()
    pump_until(lambda: "WOULD ALERT" in ed.test_summary.text(), what="test result")
    assert any("Tea $" in line for line in ed.test_result.toPlainText().splitlines())
    ed.add_rule("ignore")
    ed._set(0, 1, "selector")
    ed._set(0, 3, "li")  # ignore every list item: the change disappears
    ed.run_test_filter()
    pump_until(
        lambda: (
            "would not alert" in ed.test_summary.text() or "identical" in ed.test_summary.text()
        ),
        what="filtered",
    )


def test_add_assistant_previews_proposes_filters_and_saves(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    eng.site.set_dynamic(
        "/status",
        lambda n: (
            f"<html><body><h1>Status</h1><p>Updated {n * 3} minutes ago</p><p>All systems normal</p></body></html>"
        ),
    )
    api_client._call("POST", "/folders", json={"name": "Ops"})
    folders = api_client.folders()
    dlg = AddBookmarkDialog(api_client, folders, None, samples=2, gap_s=0)
    assert not dlg.save_btn.isEnabled()  # nothing previewed yet
    dlg.url.setText(eng.site.url("/status"))
    dlg.preview()
    pump_until(lambda: dlg.preview_out is not None, what="preview")
    out = dlg.preview_out
    assert out is not None and out.kind == "page" and out.unstable_blocks == 1
    assert "Detected a web page" in dlg.status.text() and dlg.save_btn.isEnabled()
    assert (
        dlg.proposal_list.count() == 1
        and dlg.proposal_list.item(0).checkState() == Qt.CheckState.Checked
    )
    assert dlg.name.text() == "127.0.0.1"

    dlg.folder.setCurrentIndex(dlg.folder.findData(folders[0].id))
    dlg.interval.setCurrentIndex(dlg.interval.findData(900))
    dlg.region.setChecked(True)
    dlg.selector.setText("main")
    body = dlg.build_body()
    assert body["schedule"] == {"interval_s": 900} and body["folder_id"] == folders[0].id
    assert body["filter"]["ignore"][0]["pattern_kind"] == "regex" and body["filter"]["watch"] == [
        {"type": "selector", "selector": "main"}
    ]
    created: list[Any] = []
    dlg.created.connect(created.append)
    dlg.save()
    pump_until(lambda: bool(created), what="created")
    got = api_client.bookmark(created[0].id)
    assert got.folder_id == folders[0].id and got.schedule.interval_s == 900
    assert (
        got.filter.ignore[0].note == "auto: relative_time"
        and got.filter.watch[0].selector == "main"
    )
    # an unchecked proposal is not saved
    dlg2 = AddBookmarkDialog(api_client, folders, None, samples=2, gap_s=0)
    dlg2.url.setText(eng.site.url("/status"))
    dlg2.preview()
    pump_until(lambda: dlg2.preview_out is not None)
    dlg2.proposal_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert "filter" not in dlg2.build_body()


def test_add_assistant_flags_js_apps_and_reports_fetch_errors(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    eng.site.set(
        "/app",
        "<html><head><script src='/m.js'></script></head><body><div id='root'></div></body></html>",
    )
    dlg = AddBookmarkDialog(api_client, [], None, samples=1)
    dlg.url.setText(
        eng.site.url("/app").removeprefix("http://")
    )  # scheme optional: https is assumed
    dlg.url.setText(eng.site.url("/app"))
    dlg.preview()
    pump_until(lambda: dlg.preview_out is not None)
    assert "needs a browser" in dlg.status.text() and dlg.build_body()["check_method"] == "auto"
    bad = AddBookmarkDialog(api_client, [], None, samples=1)
    bad.url.setText(eng.site.url("/nothing-here"))
    bad.preview()
    pump_until(lambda: "Could not fetch" in bad.error.text(), what="fetch error")
    assert not bad.save_btn.isEnabled()


def test_false_positive_dialog_builds_the_patch_from_the_ticked_proposals(qapp: Any) -> None:
    def prop(sel: str) -> ProposalOut:
        return ProposalOut(
            rule=FilterRule(type="selector", selector=sel), kind="element", pattern_name=None,
            explanation="this block changed", example_old="a", example_new="b", verified=True,
        )  # fmt: skip

    existing = {"type": "selector", "selector": "aside"}
    result = FalsePositiveOut(
        change_id=1, proposals=[prop("#a"), prop("#b")], resolves_all=True, remaining_changed_blocks=0,
        patch={"filter": {"ignore": [existing, {"type": "selector", "selector": "#a"}, {"type": "selector", "selector": "#b"}]}},
    )  # fmt: skip
    dlg = FalsePositiveDialog(result)
    assert dlg.list.count() == 2 and dlg.patch()["filter"]["ignore"][0] == existing
    assert [r["selector"] for r in dlg.patch()["filter"]["ignore"]] == ["aside", "#a", "#b"]
    dlg.list.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert [r["selector"] for r in dlg.patch()["filter"]["ignore"]] == [
        "aside",
        "#a",
    ]  # the user's choice
    empty = FalsePositiveDialog(
        FalsePositiveOut(
            change_id=1,
            proposals=[],
            resolves_all=False,
            remaining_changed_blocks=2,
            patch={"filter": {"ignore": []}},
        )
    )
    assert empty.patch() == {"filter": {"ignore": []}}
    QApplication.processEvents()


# -- M4: browser, screenshot and records settings in the editor --------------------------

RECORDS = {"path": "$.items", "id_field": "id", "filter": "status = Active", "fields": ["name"]}


def test_editor_round_trips_the_source_settings_without_inventing_changes(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    eng.site.set("/r.json", '{"items": [{"id": 1, "name": "a", "status": "Active"}]}',
                 content_type="application/json")  # fmt: skip
    out = api_client.create_bookmark(
        {
            "url": eng.site.url("/r.json"), "name": "Feed", "source_type": "records",
            "schedule": {"interval_s": 3600, "jitter_pct": 0},
            "fetch": {"records": RECORDS, "browser": {"scroll_count": 3, "full_page": False}},
            "filter": {"screenshot": {"min_ratio": 0.01,
                                      "ignore": [{"x": 1, "y": 2, "w": 30, "h": 40}]}},
        }
    )  # fmt: skip
    eng.settle()
    ed = BookmarkEditor(api_client, [api_client.bookmark(out.id)], api_client.folders())
    assert ed.build_patch() == {}  # saving an untouched records/screenshot bookmark sends nothing
    assert ed.b_scrolls.value() == 3 and not ed.b_full.isChecked()
    assert ed.shot_min.value() == 1.0 and ed.shot_ignore.toPlainText() == "1 2 30 40"
    assert '"id_field": "id"' in ed.records_json.toPlainText()


def test_editor_sends_only_the_changed_browser_screenshot_and_records_settings(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = make(eng, api_client)
    ed = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    assert ed.build_patch() == {}
    assert ed.b_delay.value() == 0 and ed.b_scrolls.value() == 0 and ed.b_full.isChecked()
    assert ed.shot_min.value() == 0.2 and ed.shot_height.value() == 5.0  # the specified defaults

    ed.method.setCurrentIndex(ed.method.findData("screenshot"))
    ed.b_delay.setValue(1.5)
    ed.b_scrolls.setValue(4)
    ed.shot_min.setValue(0.5)
    ed.shot_ignore.setPlainText("10 20 300 50\n0, 0, 100, 40")
    patch = ed.build_patch()
    assert patch["check_method"] == "screenshot"
    assert patch["fetch"] == {"browser": {"delay_after_load_s": 1.5, "scroll_count": 4}}
    assert patch["filter"] == {
        "screenshot": {
            "min_ratio": 0.005,
            "ignore": [{"x": 10, "y": 20, "w": 300, "h": 50}, {"x": 0, "y": 0, "w": 100, "h": 40}],
        }
    }
    ed.save()
    pump_until(lambda: api_client.bookmark(bid).fetch.browser.scroll_count == 4, what="saved")
    after = api_client.bookmark(bid)
    assert (
        after.check_method.value == "screenshot" and after.fetch.browser.delay_after_load_s == 1.5
    )
    assert after.filter.screenshot.min_ratio == 0.005 and len(after.filter.screenshot.ignore) == 2
    assert after.overrides["fetch"]["browser"] == {"delay_after_load_s": 1.5, "scroll_count": 4}


def test_editor_can_make_a_bookmark_a_records_source_and_validates_the_json(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> None:
    bid = make(eng, api_client)
    ed = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    ed.source.setCurrentIndex(ed.source.findData("records"))
    ed.records_json.setPlainText('{"id_field": ')  # not JSON
    ed.save()
    assert ed.error.text().startswith("Records configuration:")
    ed.records_json.setPlainText('{"path": "data", "id_field": "id"}')  # a bad JSONPath
    ed.save()
    assert "must start with" in ed.error.text()
    ed.records_json.setPlainText('{"id_field": "id", "filter": "status ="}')
    ed.save()
    assert "row filter" in ed.error.text()
    ed.records_json.setPlainText(json.dumps(RECORDS))
    ed.save()
    pump_until(lambda: api_client.bookmark(bid).source_type.value == "records", what="saved")
    rec = api_client.bookmark(bid).fetch.records
    assert rec is not None and rec.id_field == "id" and rec.filter == "status = Active"
    # clearing the box removes the override again
    ed2 = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    ed2.source.setCurrentIndex(ed2.source.findData("auto"))
    ed2.records_json.setPlainText("")
    assert ed2.build_patch()["fetch"] == {"records": None}


def test_editor_rejects_bad_screenshot_rectangles_and_test_filter_says_so(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    bid = make(eng, api_client)
    ed = BookmarkEditor(api_client, [api_client.bookmark(bid)], api_client.folders())
    for bad in ("1 2 3", "a b c d", "1 2 0 5", "-1 0 5 5"):
        ed.shot_ignore.setPlainText(bad)
        ed.save()
        assert ed.error.text().startswith("Screenshot rectangle on line 1"), bad
        ed.error.setText("")
    ed.run_test_filter()
    assert ed.test_summary.text().startswith("Screenshot rectangle on line 1")
