from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pagewatch.engine.pipeline import filters
from pagewatch.engine.pipeline.core import build_blocks
from pagewatch.models import FilterConfig, FilterRule


def run(html: str, **cfg: Any) -> list[str]:
    fc = FilterConfig.model_validate(cfg)
    body = html.encode()
    return [
        b.text for b in build_blocks(body, "text/html", "https://x.test/", "auto", fc, "0" * 64)
    ]


def blocks(html: str, **cfg: Any) -> list[Any]:
    fc = FilterConfig.model_validate(cfg)
    return build_blocks(html.encode(), "text/html", "https://x.test/", "auto", fc, "0" * 64)


PAGE = (
    "<html><body><nav>Home | About</nav><div id='main'><h1>Title</h1>"
    "<p class='price'>Price $19.99</p><p>Description text</p></div>"
    "<aside class='ads'>Buy now!</aside><footer>Copyright 2026</footer></body></html>"
)


# -- cosmetic and the built-in cookie list ----------------------------------------------


def test_cosmetic_selector_removes_elements_but_keeps_original_paths() -> None:
    out = blocks(PAGE, cosmetic=[{"type": "selector", "selector": "nav"}])
    assert "Home | About" not in [b.text for b in out]
    footer = next(b for b in out if b.text == "Copyright 2026")
    full = next(b for b in blocks(PAGE) if b.text == "Copyright 2026")
    assert footer.path == full.path  # skipping nav did not shift any index


def test_builtin_cookie_banner_filter_is_on_by_default_and_can_be_disabled() -> None:
    page = (
        "<body><div id='onetrust-consent-sdk'>We use cookies. Accept all</div>"
        "<div class='cookie-banner'>Cookie settings</div>"
        "<div data-x='1' class='my-cookie-consent-bar'>consent</div><p>Real content</p></body>"
    )
    assert run(page) == ["Real content"]
    assert "We use cookies. Accept all" in run(page, builtin_cosmetic=False)


def test_builtin_selector_list_loads_and_every_entry_compiles() -> None:
    sels = filters.builtin_cookie_selectors()
    assert len(sels) > 40 and "#onetrust-consent-sdk" in sels and ".cc-window" in sels


# -- ignore: selector -------------------------------------------------------------------


def test_ignore_selector_css_and_xpath() -> None:
    assert "Buy now!" not in run(PAGE, ignore=[{"type": "selector", "selector": "aside.ads"}])
    got = run(
        PAGE,
        ignore=[{"type": "selector", "selector": "//p[@class='price']", "selector_kind": "xpath"}],
    )
    assert "Price $19.99" not in got and "Description text" in got


def test_xpath_that_returns_non_elements_is_harmless() -> None:
    assert "Buy now!" in run(
        PAGE, ignore=[{"type": "selector", "selector": "//p/@class", "selector_kind": "xpath"}]
    )


# -- watch ------------------------------------------------------------------------------


def test_watch_selector_keeps_only_the_region() -> None:
    assert run(PAGE, watch=[{"type": "selector", "selector": "#main p.price"}]) == ["Price $19.99"]
    assert run(PAGE, watch=[{"type": "selector", "selector": "#main"}]) == [
        "Title", "Price $19.99", "Description text"
    ]  # fmt: skip


def test_watch_inline_element_inside_a_block_is_kept_on_its_own() -> None:
    page = "<p>Our price is <span class='p'>$5</span> today only</p>"
    assert run(page, watch=[{"type": "selector", "selector": "span.p"}]) == ["$5"]


def test_several_watch_rules_union_in_document_order() -> None:
    got = run(
        PAGE,
        watch=[
            {"type": "selector", "selector": "footer"},
            {"type": "selector", "selector": "h1"},
            {"type": "selector", "selector": "h1"},  # duplicates collapse
        ],
    )
    assert got == ["Title", "Copyright 2026"]


def test_watch_region_that_vanishes_gives_an_empty_page() -> None:
    assert run(PAGE, watch=[{"type": "selector", "selector": "#gone"}]) == []


def test_watch_nested_matches_are_not_duplicated() -> None:
    got = run(PAGE, watch=[{"type": "selector", "selector": "#main, #main p"}])
    assert got == ["Title", "Price $19.99", "Description text"]


# -- between ----------------------------------------------------------------------------

DOC = "<p>Intro</p><h2>Latest news</h2><p>story one</p><p>story two</p><h2>Archive</h2><p>old</p>"


def test_between_ignore_excludes_markers_by_default_and_includes_on_request() -> None:
    rule = {"type": "between", "start": "Latest news", "end": "Archive"}
    assert run(DOC, ignore=[rule]) == ["Intro", "Latest news", "Archive", "old"]
    assert run(DOC, ignore=[{**rule, "inclusive": True}]) == ["Intro", "old"]


def test_between_open_ended_ranges() -> None:
    assert run(DOC, ignore=[{"type": "between", "end": "Latest news"}]) == [
        "Latest news", "story one", "story two", "Archive", "old"
    ]  # fmt: skip
    assert run(DOC, ignore=[{"type": "between", "start": "Archive"}]) == [
        "Intro", "Latest news", "story one", "story two", "Archive"
    ]  # fmt: skip


def test_between_watch_keeps_only_the_range() -> None:
    assert run(DOC, watch=[{"type": "between", "start": "Latest news", "end": "Archive"}]) == [
        "story one", "story two"
    ]  # fmt: skip


def test_between_markers_are_case_insensitive_and_can_sit_inside_a_block() -> None:
    doc = "<p>head START of stuff middle END tail</p><p>after</p>"
    assert run(doc, ignore=[{"type": "between", "start": "start", "end": "end"}]) == [
        "head START END tail", "after"
    ]  # fmt: skip


def test_between_with_a_missing_marker_changes_nothing() -> None:
    # a restructured page must not silently swallow everything after the start marker
    assert run(DOC, ignore=[{"type": "between", "start": "Latest news", "end": "Nowhere"}]) == run(
        DOC
    )
    assert run(DOC, ignore=[{"type": "between", "start": "Nope", "end": "Archive"}]) == run(DOC)


def test_between_repeats_for_every_occurrence() -> None:
    doc = "<p>[c] one [/c]</p><p>keep</p><p>[c] two [/c]</p>"
    assert run(
        doc, ignore=[{"type": "between", "start": "[c]", "end": "[/c]", "inclusive": True}]
    ) == ["keep"]


def test_between_offsets_survive_characters_whose_lowercase_changes_length() -> None:
    doc = "<p>İİİ MARK secret END ok</p>"  # 'İ'.lower() is two code points
    assert run(doc, ignore=[{"type": "between", "start": "mark", "end": "end"}]) == [
        "İİİ MARK END ok"
    ]


def test_empty_between_marker_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FilterRule(type="between", start="  ")


# -- ignore: text and number_mask -------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "pattern", "text", "expected"),
    [
        ("literal", "(updated)", "Page (updated) now", "Page now"),
        ("wildcard", "Updated * ago", "News Updated 5 minutes ago end", "News end"),
        ("wildcard", "v?.?", "release v2.1 notes", "release notes"),
        ("regex", r"\d+ views", "Hits 1204 views today", "Hits today"),
        ("wildcard", "build *", "x build 77 y", "x"),  # trailing * runs to the end
        ("regex", r"\b[0-9a-f]{16,}\b", "token deadbeefdeadbeef01 here", "token here"),
        ("literal", "BUY", "buy it now", "it now"),  # ignore_case default
    ],
)
def test_text_ignore_kinds(kind: str, pattern: str, text: str, expected: str) -> None:
    assert run(
        f"<p>{text}</p>", ignore=[{"type": "text", "pattern": pattern, "pattern_kind": kind}]
    ) == [expected]


def test_text_ignore_that_empties_a_block_drops_it() -> None:
    assert run(
        "<p>Updated 5 min ago</p><p>real</p>",
        ignore=[{"type": "text", "pattern": "Updated * ago", "pattern_kind": "wildcard"}],
    ) == ["real"]


def test_text_ignore_is_case_sensitive_when_ignore_case_is_off() -> None:
    cfg = {"special": {"ignore_case": False}, "ignore": [{"type": "text", "pattern": "buy"}]}
    assert run("<p>BUY and buy</p>", **cfg) == ["BUY and"]


def test_scoped_text_ignore_only_touches_blocks_inside_the_scope() -> None:
    page = "<div class='meta'><p>12 views</p></div><p>12 views of the report</p>"
    got = run(
        page,
        ignore=[
            {"type": "text", "pattern": r"\d+ views", "pattern_kind": "regex", "scope": ".meta"}
        ],
    )
    assert got == ["of the report"[:0] or "12 views of the report"] or got == [
        "12 views of the report"
    ]
    assert "12 views of the report" in got and "12 views" not in got


def test_scope_works_for_an_inline_element_inside_a_block() -> None:
    page = "<p>Posted <span class='n'>42 views</span> by Sam</p><p>42 views elsewhere</p>"
    got = run(page, ignore=[{"type": "text", "pattern": "42 views", "scope": "span.n"}])
    assert got == ["Posted by Sam", "42 views elsewhere"]


def test_scope_that_matches_nothing_is_a_noop() -> None:
    assert run(
        "<p>12 views</p>", ignore=[{"type": "text", "pattern": "views", "scope": ".nope"}]
    ) == ["12 views"]


def test_number_mask_everywhere_in_scope_or_by_pattern() -> None:
    page = "<p class='c'>Visitors: 1204</p><p>Room 12 costs $40</p>"
    assert run(page, ignore=[{"type": "number_mask"}]) == ["Visitors: ####", "Room ## costs $##"]
    assert run(page, ignore=[{"type": "number_mask", "scope": ".c"}]) == [
        "Visitors: ####",
        "Room 12 costs $40",
    ]
    assert run(page, ignore=[{"type": "number_mask", "pattern": "Visitors"}]) == [
        "Visitors: ####",
        "Room 12 costs $40",
    ]


def test_masked_counter_makes_two_versions_identical() -> None:
    a = run("<p>Visitors: 1204</p>", ignore=[{"type": "number_mask", "pattern": "visitors"}])
    b = run("<p>Visitors: 1317</p>", ignore=[{"type": "number_mask", "pattern": "visitors"}])
    assert a == b


# -- non-HTML sources and failure handling ----------------------------------------------


def test_text_filters_apply_to_non_html_sources_and_selectors_are_skipped() -> None:
    fc = FilterConfig.model_validate({
        "ignore": [{"type": "text", "pattern": "build *", "pattern_kind": "wildcard"},
                   {"type": "selector", "selector": "div"}],
        "watch": [{"type": "selector", "selector": "#x"},  # no DOM: cannot apply, so no watch at all
                  {"type": "between", "start": "BEGIN", "end": "END"}],
    })  # fmt: skip
    body = b"junk\nBEGIN\nvalue 1\nbuild 77\nvalue 2\nEND\nmore junk"
    got = build_blocks(body, "text/plain", "https://x/", "auto", fc, "0" * 64)
    assert [b.text for b in got] == ["value 1", "value 2"]


def test_invalid_selectors_and_keywords_are_rejected_at_the_model_boundary() -> None:
    for bad in (
        {"ignore": [{"type": "selector", "selector": "div[["}]},
        {"ignore": [{"type": "selector", "selector": "//div[", "selector_kind": "xpath"}]},
        {"ignore": [{"type": "text", "pattern": "x", "scope": "p["}]},
        {"cosmetic": [{"type": "text", "pattern": "x"}]},
        {"watch": [{"type": "number_mask"}]},
    ):
        with pytest.raises(ValidationError):
            FilterConfig.model_validate(bad)


def test_a_rule_that_fails_at_run_time_is_skipped_with_a_warning() -> None:
    warnings: list[str] = []
    fc = FilterConfig.model_construct(  # bypass validation to simulate stored-before-fix config
        cosmetic=[FilterRule.model_construct(type="selector", selector="div[[", selector_kind="css")],
        builtin_cosmetic=False, watch=[], ignore=[],
        special=FilterConfig().special,
    )  # fmt: skip
    got = build_blocks(
        b"<p>still works</p>", "text/html", "https://x/", "auto", fc, "0" * 64, warnings.append
    )
    assert [b.text for b in got] == ["still works"] and len(warnings) == 1


def test_filters_run_before_special_filters_so_sorting_sees_the_filtered_page() -> None:
    page = "<p>b item</p><aside>zzz</aside><p>a item</p>"
    got = run(
        page, ignore=[{"type": "selector", "selector": "aside"}], special={"sort_content": True}
    )
    assert got == ["a item", "b item"]
