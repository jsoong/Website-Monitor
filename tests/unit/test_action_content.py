"""What an alert says about a change: the worker-side content builder and the email-safe HTML."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from lxml import html as lxml_html

from pagewatch.engine.actions import htmlmail
from pagewatch.engine.actions.base import AlertContext
from pagewatch.engine.actions.content import (
    ContentJob,
    ContentResult,
    alert_vars,
    build_content,
)
from pagewatch.engine.pipeline.diff import DiffResult
from tests.support.pipeline_harness import Harness

OLD = (
    "<html><body><h1>Prices</h1><p>Widget costs $19 today</p><p>Gadget in stock</p>"
    "<p>Sold out: Gizmo</p><footer>Contact us</footer></body></html>"
)
NEW = (
    "<html><body><h1>Prices</h1><p>Brand new Doohickey arrives</p>"
    "<p>Widget costs $17 today</p><p>Gadget in stock</p><footer>Contact us</footer></body></html>"
)


def job(h: Harness, res: Any, **over: Any) -> ContentJob:
    assert h.latest is not None
    old = h.versions[h.latest.version_id - 1]
    base: dict[str, Any] = {
        "blob_root": str(h.root),
        "url": "https://example.com/",
        "new_blocks_hash": h.latest.blocks_hash,
        "old_blocks_hash": old.blocks_hash,
        "diff_hash": res.diff_hash,
        "new_raw_hash": h.latest.raw_hash,
        "content_type": "text/html",
        "source_type": "auto",
        "source_cfg": {},
        "filter_cfg": {},
        "highlight_mode": "standard",
    }
    return ContentJob(**{**base, **over})


@pytest.fixture
def changed(tmp_path: Path) -> tuple[Harness, Any]:
    h = Harness(tmp_path)
    h.run(OLD)
    res = h.run(NEW)
    assert res.alert
    return h, res


def test_content_has_added_and_removed_text_and_a_plain_diff(changed: tuple[Harness, Any]) -> None:
    h, res = changed
    out = build_content(job(h, res))
    assert "Brand new Doohickey arrives" in out.added_text
    assert "$17" in out.added_text
    assert "Sold out: Gizmo" in out.removed_text
    assert "$19" in out.removed_text
    lines = out.changes_text.splitlines()
    assert any("Brand new Doohickey arrives" in ln and ln[:1] in "+~" for ln in lines)
    assert any(ln.startswith("- Sold out: Gizmo") for ln in lines)
    assert any(ln.startswith("~ ") and "[-" in ln and "{+" in ln for ln in lines)
    assert out.stats["added_words"] > 0 and not out.degraded
    assert out.diff["ops"]


def test_changes_html_is_email_safe_and_shows_only_the_changes(
    changed: tuple[Harness, Any],
) -> None:
    h, res = changed
    out = build_content(job(h, res))
    assert "Brand new Doohickey" in out.changes_html and "Sold out: Gizmo" in out.changes_html
    assert "Contact us" not in out.changes_html  # unchanged blocks are not part of "changes only"
    assert_email_safe(out.changes_html)
    assert re.search(r'<ins style="[^"]*background:#d9f7d9', out.changes_html)
    assert re.search(r'<del style="[^"]*line-through', out.changes_html)


def assert_email_safe(markup: str) -> None:
    assert "<style" not in markup and "<script" not in markup
    assert " class=" not in markup and " id=" not in markup
    assert "src=" not in markup  # nothing remote to load
    root = lxml_html.fragment_fromstring(markup, create_parent="div")
    for el in root.iter("ins", "del"):
        assert el.get("style"), "every mark carries its own inline style"


def test_full_html_is_the_highlighted_page_with_inline_styles(changed: tuple[Harness, Any]) -> None:
    h, res = changed
    out = build_content(job(h, res, want_full=True))
    assert out.full_html is not None
    assert "Contact us" in out.full_html  # the whole page, unlike "changes only"
    assert "Prices" in out.full_html
    assert_email_safe(out.full_html)
    assert "pw-deleted" not in out.full_html  # the side panel of deletions is dropped
    assert "Sold out: Gizmo" in out.full_html  # ...because deletions are shown in place


def test_full_html_is_none_when_the_source_is_not_html(tmp_path: Path) -> None:
    h = Harness(tmp_path, source_type="auto")
    h.run('{"a": 1}', ctype="application/json")
    res = h.run('{"a": 2}', ctype="application/json")
    assert res.alert
    out = build_content(job(h, res, want_full=True, content_type="application/json"))
    assert out.full_html is None
    assert out.changes_text  # the text view still works


def test_oversized_changes_are_counted_not_dumped(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run("<html><body><p>start</p></body></html>")
    items = "".join(f"<p>new paragraph number {i}</p>" for i in range(30))
    res = h.run(f"<html><body><p>start</p>{items}</body></html>")
    out = build_content(job(h, res, max_blocks=10))
    assert out.changes_html.count("new paragraph number") == 10
    assert out.changes_hidden == 20


def test_first_check_content_treats_the_whole_page_as_added(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    res = h.run("<html><body><p>hello world</p></body></html>")
    assert h.latest is not None
    out = build_content(
        ContentJob(
            blob_root=str(tmp_path),
            url="https://e.com/",
            new_blocks_hash=h.latest.blocks_hash,
            old_blocks_hash=None,
            diff_hash=res.diff_hash,
            new_raw_hash=h.latest.raw_hash,
            content_type="text/html",
            source_type="auto",
            source_cfg={},
            filter_cfg={},
            highlight_mode="standard",
        )  # fmt: skip
    )
    assert out.added_text == "hello world" and out.removed_text == ""


def test_removed_regions_covers_whole_blocks_and_deleted_words() -> None:
    diff = DiffResult(
        ops=[
            {"t": "del", "old": [0, 0]},
            {"t": "rep", "old": [1, 1], "new": [0, 0],
             "tokens": [["eq", "price "], ["del", "$19"], ["ins", "$17"]]},
            {"t": "rep", "old": [2, 2], "new": [1, 1]},
        ],
        stats={},
    )  # fmt: skip
    assert diff.removed_regions(["gone", "price $19", "old block"]) == [
        "gone", "$19", "old block",
    ]  # fmt: skip


# -- template variables ---------------------------------------------------------------------


def ctx(private: bool = False) -> AlertContext:
    return AlertContext(
        bookmark_id=3, name="Rent", url="https://example.com/secret?token=abc",
        info=("alpha-info", "beta-info", None), change_id=9, detected_at="2026-11-05T14:30:07.000000Z",
        summary="Studio $1,200", added_words=3, removed_words=1, changed_blocks=1,
        checks_accumulated=2, keyword_hits=["studio"], private=private,
    )  # fmt: skip


def test_alert_vars_for_a_normal_alert_expose_the_change() -> None:
    content = ContentResult(added_text="studio added", stats={"added_words": 3})
    v = alert_vars(ctx(), content)
    assert v["bookmark"]["url"].startswith("https://") and v["bookmark"]["info1"] == "alpha-info"
    assert v["change"]["added_text"] == "studio added"
    assert v["change"]["keyword_hits"] == ["studio"] and v["change"]["checks_accumulated"] == 2
    assert v["private"] is False


def test_alert_vars_for_a_private_alert_carry_the_name_and_ids_and_nothing_else() -> None:
    leaky = ContentResult(added_text="SECRET PAGE TEXT", removed_text="MORE", stats={"a": 1})
    v = alert_vars(ctx(private=True), leaky)  # even if content was somehow passed in
    flat = repr(v)
    for secret in (
        "SECRET PAGE TEXT",
        "MORE",
        "token=abc",
        "Studio",
        "studio",
        "alpha-info",
        "beta-info",
    ):
        assert secret not in flat
    assert v["bookmark"]["name"] == "Rent" and v["bookmark"]["id"] == 3
    assert v["change"]["id"] == 9 and v["private"] is True


# -- inline_marks ---------------------------------------------------------------------------


def test_inline_marks_converts_classes_and_drops_the_side_panel() -> None:
    src = (
        '<p>a <ins class="pw-add">new</ins> <del class="pw-del">old</del></p>'
        '<table><tr class="pw-rep-block"><td class="pw-changed-cell">x</td></tr></table>'
        '<div id="pw-deleted"><del>panel</del></div>'
    )
    out = htmlmail.inline_marks(src)
    assert "panel" not in out
    assert_email_safe(out)
    assert "background:#d9f7d9" in out and "line-through" in out
    assert out.count("<td") == 1 and "border-left" in out  # the row mark became a style
