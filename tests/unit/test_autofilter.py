from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine.pipeline import autofilter
from pagewatch.engine.pipeline.extract import parse_html
from pagewatch.engine.store.blobs import BlobStore


def propose(
    tmp_path: Path, old: str, new: str, *, ctype: str = "text/html", **cfg: Any
) -> autofilter.ProposalResult:
    store = BlobStore(tmp_path)
    a, b = store.put(old.encode()), store.put(new.encode())
    return autofilter.propose(
        store, old_raw=a, new_raw=b, old_ctype=ctype, new_ctype=ctype, url="https://x.test/",
        source_type="auto", filter_cfg=cfg.get("filter", {}), highlight_mode="standard",
    )  # fmt: skip


def page(*blocks: str) -> str:
    return "<html><body>" + "".join(blocks) + "</body></html>"


# -- the shipped pattern list -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "text", "hit"),
    [
        ("relative_time", "Updated 5 minutes ago", True),
        ("relative_time", "posted just now", True),
        ("relative_time", "Updated at lunch", False),
        ("iso_datetime", "2026-10-05", True),
        ("iso_datetime", "2026-10-05T13:10:00Z", True),
        ("long_date", "Oct 5, 2026", True),
        ("long_date", "Monday, October 5", True),
        ("long_date", "5 October 2026", True),
        ("numeric_date", "10/05/2026", True),
        ("numeric_date", "05.10.26", True),
        ("clock_time", "1:07 PM", True),
        ("clock_time", "13:10:44 UTC", True),
        ("counter", "1,204 views", True),
        ("counter", "37 visitors online", True),
        ("counter", "Downloads: 5,512", True),
        ("counter", "views of the report", False),
        ("unix_timestamp", "1759669800", True),
        ("unix_timestamp", "1759669800123", True),
        ("hex_token", "deadbeefdeadbeef0123", True),
        ("hex_token", "deadbeef", False),
        ("currency_amount", "$1,299.00", True),
        ("currency_amount", "12,50 €", True),
    ],
)
def test_shipped_volatile_patterns(name: str, text: str, hit: bool) -> None:
    pat = next(p for p in autofilter.volatile_patterns() if p.name == name)
    assert bool(pat.regex.search(text)) is hit


def test_every_pattern_is_documented_and_compiles() -> None:
    names = [p.name for p in autofilter.volatile_patterns()]
    assert len(names) == len(set(names)) >= 9
    assert all(p.description for p in autofilter.volatile_patterns())


# -- proposals --------------------------------------------------------------------------


def test_relative_timestamp_becomes_a_verified_text_rule(tmp_path: Path) -> None:
    res = propose(
        tmp_path,
        page("<p>Updated 5 minutes ago</p><p>news</p>"),
        page("<p>Updated 9 minutes ago</p><p>news</p>"),
    )
    (p,) = res.proposals
    assert (p.kind, p.pattern_name, p.verified) == ("volatile_pattern", "relative_time", True)
    assert p.rule["type"] == "text" and p.rule["pattern_kind"] == "regex"
    assert res.resolves_all and res.remaining_changed_blocks == 0
    assert "5 minutes" in p.example_old and "9 minutes" in p.example_new


@pytest.mark.parametrize(
    ("old", "new", "pattern"),
    [
        ("Posted 2026-10-05", "Posted 2026-10-06", "iso_datetime"),
        ("1,204 views", "1,317 views", "counter"),
        ("Server time 13:10:44", "Server time 13:10:59", "clock_time"),
        ("session deadbeefdeadbeef0123", "session cafebabecafebabe4567", "hex_token"),
    ],
)
def test_other_volatile_patterns_are_recognised(
    tmp_path: Path, old: str, new: str, pattern: str
) -> None:
    res = propose(tmp_path, page(f"<p>{old}</p>"), page(f"<p>{new}</p>"))
    assert [p.pattern_name for p in res.proposals] == [pattern] and res.resolves_all


def test_a_real_edit_gets_an_element_rule_not_a_text_pattern(tmp_path: Path) -> None:
    res = propose(
        tmp_path,
        page("<p id='quote'>carpe diem</p><p>stable</p>"),
        page("<p id='quote'>seize the day</p><p>stable</p>"),
    )
    (p,) = res.proposals
    assert p.kind == "element" and p.rule == {
        "type": "selector",
        "selector": "#quote",
        "note": "auto: element",
    }
    assert p.verified and res.resolves_all


def test_element_without_a_stable_css_selector_falls_back_to_an_absolute_xpath(
    tmp_path: Path,
) -> None:
    res = propose(
        tmp_path, page("<p>one</p><p>two</p><p>three</p>"), page("<p>one</p><p>2</p><p>three</p>")
    )
    (p,) = res.proposals
    assert p.rule["selector_kind"] == "xpath" and p.rule["selector"] == "/html[1]/body[1]/p[2]"
    assert p.verified


def test_scope_is_added_when_a_stable_selector_exists(tmp_path: Path) -> None:
    res = propose(
        tmp_path,
        page("<div id='meta'><p>Updated 5 minutes ago</p></div><p>Updated 1 hour ago</p>"),
        page("<div id='meta'><p>Updated 9 minutes ago</p></div><p>Updated 1 hour ago</p>"),
    )
    (p,) = res.proposals
    assert (
        p.rule["scope"] == "div#meta > p"
        or p.rule["scope"] == "#meta > p"
        or "meta" in p.rule["scope"]
    )
    assert p.verified


def test_two_independent_false_positives_need_both_rules(tmp_path: Path) -> None:
    res = propose(
        tmp_path,
        page("<p>Updated 5 minutes ago</p><p id='ad'>Buy shoes</p><p>stable</p>"),
        page("<p>Updated 9 minutes ago</p><p id='ad'>Buy hats</p><p>stable</p>"),
    )
    assert len(res.proposals) == 2
    assert (
        all(p.verified for p in res.proposals) is False or res.resolves_all
    )  # each fixes only its block
    assert res.resolves_all and res.remaining_changed_blocks == 0
    assert sorted(p.kind for p in res.proposals) == ["element", "volatile_pattern"]


def test_inserted_and_deleted_blocks_get_element_rules(tmp_path: Path) -> None:
    res = propose(
        tmp_path, page("<p id='a'>kept</p><div id='promo'>Sale!</div>"), page("<p id='a'>kept</p>")
    )
    assert [p.rule["selector"] for p in res.proposals] == ["#promo"]
    res = propose(
        tmp_path, page("<p id='a'>kept</p>"), page("<p id='a'>kept</p><div id='banner'>New!</div>")
    )
    assert [p.rule["selector"] for p in res.proposals] == ["#banner"]


def test_no_proposals_when_there_is_no_dom_to_point_at(tmp_path: Path) -> None:
    res = propose(tmp_path, "line one\nline two", "line one\nline three", ctype="text/plain")
    assert res.proposals == [] and res.resolves_all is False and res.remaining_changed_blocks > 0


def test_proposals_are_added_on_top_of_the_current_filters(tmp_path: Path) -> None:
    flt = {"ignore": [{"type": "selector", "selector": "aside"}]}
    res = propose(
        tmp_path,
        page("<aside>x1</aside><p>Updated 5 minutes ago</p>"),
        page("<aside>x2</aside><p>Updated 9 minutes ago</p>"),
        filter=flt,
    )
    assert [p.pattern_name for p in res.proposals] == ["relative_time"] and res.resolves_all


def test_identical_pages_yield_nothing(tmp_path: Path) -> None:
    res = propose(tmp_path, page("<p>same</p>"), page("<p>same</p>"))
    assert res.proposals == [] and res.remaining_changed_blocks == 0


# -- selector derivation ----------------------------------------------------------------


def sel(html: str, css: str) -> str | None:
    root = parse_html(html)
    el = root.cssselect(css)[0] if hasattr(root, "cssselect") else None
    if el is None:
        from lxml.cssselect import CSSSelector

        el = CSSSelector(css)(root)[0]
    return autofilter.css_selector_for(el, root)


def test_selector_prefers_id_then_unique_class_then_ancestor_chain() -> None:
    assert sel("<div id='main'><p>x</p></div>", "#main") == "#main"
    assert sel("<div><p class='price'>1</p><p>2</p></div>", ".price") == "p.price"
    assert (
        sel("<div id='a'><span>1</span></div><div id='b'><span>2</span></div>", "#b span")
        == "#b > span"
    )
    assert sel("<p>1</p><p>2</p>", "p") is None  # nothing stable to hold on to


def test_generated_class_names_are_not_used() -> None:
    html = "<div class='css-1a2b3c9'><p class='price'>1</p></div><div class='css-9f8e7d6'><p class='price'>2</p></div>"
    assert sel(html, "div") is None or "css-" not in (sel(html, "div") or "")
