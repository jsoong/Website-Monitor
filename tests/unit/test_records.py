"""Records sources: JSONPath subset, row filter, rows, record blocks and events."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from pagewatch.engine.pipeline import records as rec
from pagewatch.engine.pipeline.core import build_blocks
from pagewatch.models import FilterConfig, RecordsConfig

HASH = "0" * 64


def cfg(**kw: Any) -> RecordsConfig:
    kw.setdefault("id_field", "id")
    return RecordsConfig(**kw)


# -- JSONPath ---------------------------------------------------------------------------

DOC = {
    "data": {"lotteries": [{"id": 1, "a": {"b": [10, 20]}}, {"id": 2, "a": {"b": [30]}}]},
    "meta": {"count": 2},
    "by_id": {"7": {"name": "x"}, "8": {"name": "y"}},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$", [DOC]),
        ("$.meta.count", [2]),
        ("$['meta']['count']", [2]),
        ('$["meta"].count', [2]),
        ("$.data.lotteries[0].id", [1]),
        ("$.data.lotteries[-1].id", [2]),
        ("$.data.lotteries[*].id", [1, 2]),
        ("$.data.lotteries.*.id", [1, 2]),
        ("$..b", [[10, 20], [30]]),
        ("$..id", [1, 2]),
        ("$.data.lotteries[5]", []),
        ("$.missing.deeper", []),
        ("$.meta.count.deeper", []),
    ],
)
def test_jsonpath_subset(path: str, expected: list[Any]) -> None:
    assert rec.evaluate_path(rec.parse_path(path), DOC) == expected


@pytest.mark.parametrize(
    "bad", ["data.x", "", "$.", "$..", "$[", "$[abc]", "$[1:3]", "$.a b[", "$ x"]
)
def test_bad_jsonpath_is_a_syntax_error(bad: str) -> None:
    with pytest.raises(rec.RecordsSyntaxError):
        rec.parse_path(bad)


# -- row filter -------------------------------------------------------------------------

ROWS = [
    {"id": 1, "borough": "MN", "status": "Active", "units": 40, "addr": {"zip": "10001"}},
    {"id": 2, "borough": "BK", "status": "Closed", "units": 12, "addr": {"zip": "11201"}},
    {"id": 3, "borough": "BX", "status": "Active", "units": 7},
    {"id": 4, "borough": "QN", "status": "active", "units": "1,500"},
]


@pytest.mark.parametrize(
    ("expr", "ids"),
    [
        ("status = Active", [1, 3, 4]),  # case-insensitive
        ("status == 'Active'", [1, 3, 4]),
        ("status != Active", [2]),
        ("borough in [MN, BK]", [1, 2]),
        ("borough not in [MN, BK]", [3, 4]),
        ("borough in [\"MN\", 'BX']", [1, 3]),
        ("borough in [MN, BK] and status = Active", [1]),
        ("borough = MN or borough = BX", [1, 3]),
        ("status = Active and units > 10", [1, 4]),  # 1,500 parses as a number
        ("units >= 12 and units < 100", [1, 2]),
        ("addr.zip = 10001", [1]),
        ("addr.zip contains 112", [2]),
        ("borough startswith B", [2, 3]),
        ("borough endswith N", [1, 4]),
        ("not status = Active", [2]),
        ("missing = x", []),
        ("missing != x", [1, 2, 3, 4]),
        ("status = Active and borough = MN or borough = QN", [1, 4]),
    ],
)
def test_row_filter(expr: str, ids: list[int]) -> None:
    keep = rec.parse_row_filter(expr)
    assert [r["id"] for r in ROWS if keep(r)] == ids


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "status =",
        "= x",
        "status",
        "status in x",
        "status in [a",
        "and",
        "a = b and",
        "a ~ b",
        "a = b c",
        "not",
        "a in []]",
    ],
)
def test_bad_row_filter_is_a_syntax_error(bad: str) -> None:
    with pytest.raises(rec.RecordsSyntaxError):
        rec.parse_row_filter(bad)


def test_config_rejects_bad_path_and_filter_at_the_model_boundary() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        cfg(path="data")
    with pytest.raises(ValidationError, match="row filter"):
        cfg(filter="status =")
    with pytest.raises(ValidationError):
        cfg(id_field="")
    with pytest.raises(ValidationError):
        cfg(events=[])
    with pytest.raises(ValidationError):
        cfg(events=["created"])  # type: ignore[list-item]


# -- rows -------------------------------------------------------------------------------


def test_json_rows_by_path_with_filter() -> None:
    c = cfg(path="$.data.items", filter="status = Active")
    doc = {"data": {"items": [{"id": 1, "status": "Active"}, {"id": 2, "status": "Closed"}]}}
    assert rec.load_rows(json.dumps(doc), c) == [{"id": 1, "status": "Active"}]


def test_root_array_and_id_keyed_object() -> None:
    assert [r["id"] for r in rec.load_rows('[{"id": 1}, {"id": 2}]', cfg())] == [1, 2]
    rows = rec.load_rows(json.dumps(DOC), cfg(path="$.by_id"))
    assert rows == [{"id": "7", "name": "x"}, {"id": "8", "name": "y"}]


def test_csv_rows_with_delimiter_and_header_check() -> None:
    text = "id;name;status\n1;Sunset;Active\n2;Harbor;Closed\n"
    assert [r["name"] for r in rec.load_rows(text, cfg(format="csv", delimiter=";"))] == [
        "Sunset",
        "Harbor",
    ]
    with pytest.raises(rec.RecordsError, match="no column 'id'"):
        rec.load_rows("a,b\n1,2\n", cfg(format="csv"))
    with pytest.raises(rec.RecordsError, match="no header"):
        rec.load_rows("", cfg(format="csv"))


def test_format_auto_detection() -> None:
    assert rec.load_rows('[{"id": 1}]', cfg(), content_type="application/json")
    assert rec.load_rows("id,n\n1,a\n", cfg(), content_type="text/csv")
    assert rec.load_rows("id,n\n1,a\n", cfg(), url="https://x/data.csv")
    assert rec.load_rows('{"x": 1}', cfg(path="$"), url="https://x/y") == [{"x": 1}]  # json by "{"
    assert rec.load_rows("id,n\n1,a\n", cfg())[0]["n"] == "a"  # csv by elimination


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("not json", "not valid JSON"),
        ('{"other": []}', "matched nothing"),
        ('{"data": 5}', "does not select rows"),
    ],
)
def test_wrong_shape_fails_the_check_instead_of_removing_every_record(
    text: str, match: str
) -> None:
    with pytest.raises(rec.RecordsError, match=match):
        rec.load_rows(text, cfg(path="$.data", format="json"))


def test_non_object_rows_are_skipped_with_a_warning() -> None:
    warnings: list[str] = []
    rows = rec.load_rows('[{"id": 1}, 5, "x", null]', cfg(), warn=warnings.append)
    assert rows == [{"id": 1}] and any("3 rows were not objects" in w for w in warnings)


# -- record blocks ----------------------------------------------------------------------


def test_block_is_id_then_watched_fields_in_configured_order() -> None:
    c = cfg(id_field="lottery_id", fields=["status", "name"])
    row = {"lottery_id": 42, "name": "Sunset", "status": "Active", "noise": "x"}
    (block,) = rec.record_blocks([row], c)
    assert block.text == "lottery_id: 42 | status: Active | name: Sunset"
    assert block.path == "record:42" and block.kind == "record"


def test_default_fields_are_every_other_field_sorted_so_key_order_is_irrelevant() -> None:
    a = rec.record_blocks([{"id": 1, "b": 2, "a": 1}], cfg())
    b = rec.record_blocks([{"a": 1, "id": 1, "b": 2}], cfg())
    assert a[0].text == b[0].text == "id: 1 | a: 1 | b: 2"


def test_blocks_are_sorted_by_id_so_a_reordering_feed_is_not_a_change() -> None:
    rows = [{"id": 10}, {"id": 9}, {"id": 100}, {"id": "abc"}, {"id": "Abd"}]
    assert [b.path for b in rec.record_blocks(rows, cfg())] == [
        "record:9",
        "record:10",
        "record:100",
        "record:abc",
        "record:Abd",
    ]


def test_missing_and_duplicate_ids_are_skipped_with_warnings() -> None:
    warnings: list[str] = []
    rows = [{"id": 1, "v": "first"}, {"id": 1, "v": "second"}, {"v": "no id"}, {"id": ""}]
    blocks = rec.record_blocks(rows, cfg(), warn=warnings.append)
    assert [b.text for b in blocks] == ["id: 1 | v: first"]
    assert any("had no 'id'" in w for w in warnings) and any(
        "repeated an ID" in w for w in warnings
    )


def test_value_formatting_is_stable() -> None:
    row = {"id": 1, "n": 3.0, "f": 2.5, "t": True, "none": None, "d": {"b": 1, "a": 2}, "l": [1, 2]}
    (block,) = rec.record_blocks([row], cfg(fields=["n", "f", "t", "none", "d", "l"]))
    assert block.text == 'id: 1 | n: 3 | f: 2.5 | t: true | none: | d: {"a":2,"b":1} | l: [1,2]'


def test_nested_fields_and_id() -> None:
    c = cfg(id_field="meta.key", fields=["meta.state"])
    (block,) = rec.record_blocks([{"meta": {"key": "k1", "state": "open"}}], c)
    assert block.text == "meta.key: k1 | meta.state: open"


# -- events -----------------------------------------------------------------------------


def blocks_of(rows: list[dict[str, Any]], **kw: Any) -> list[Any]:
    return rec.record_blocks(rows, cfg(**kw))


def test_events_new_changed_removed() -> None:
    old = blocks_of([{"id": 1, "s": "Active"}, {"id": 2, "s": "Active"}, {"id": 3, "s": "Active"}])
    new = blocks_of([{"id": 1, "s": "Active"}, {"id": 2, "s": "Closed"}, {"id": 4, "s": "Active"}])
    ev = rec.record_events(old, new)
    assert (ev.new, ev.changed, ev.removed) == (["4"], ["2"], ["3"])
    assert ev.kinds() == {"new", "changed", "removed"} and ev


def test_no_events_when_nothing_differs_and_case_only_edits_follow_ignore_case() -> None:
    old = blocks_of([{"id": 1, "s": "Active"}])
    assert not rec.record_events(old, blocks_of([{"id": 1, "s": "Active"}]))
    assert not rec.record_events(old, blocks_of([{"id": 1, "s": "ACTIVE"}]), ignore_case=True)
    assert rec.record_events(old, blocks_of([{"id": 1, "s": "ACTIVE"}]), ignore_case=False).changed


def test_watched_fields_only_so_other_fields_are_not_changes() -> None:
    watch = {"fields": ["status"]}
    old = blocks_of([{"id": 1, "status": "Active", "views": 10}], **watch)
    new = blocks_of([{"id": 1, "status": "Active", "views": 99}], **watch)
    assert not rec.record_events(old, new)


def test_description_names_the_records() -> None:
    old = blocks_of([{"id": 3, "name": "Gone"}])
    new = blocks_of([{"id": 4, "name": "Sunset Terrace"}, {"id": 5, "name": "Harbor View"}])
    ev = rec.record_events(old, new)
    text = rec.describe_events(ev, new, old)
    assert "New (2): id: 4 | name: Sunset Terrace; id: 5 | name: Harbor View" in text
    assert "Removed: id: 3 | name: Gone" in text


def test_description_is_bounded_for_many_records() -> None:
    new = blocks_of([{"id": i} for i in range(1, 11)])
    text = rec.describe_events(rec.record_events([], new), new, [])
    assert "(+7 more)" in text and "New (10)" in text


# -- through build_blocks ---------------------------------------------------------------


def test_build_blocks_for_a_records_source_applies_ignore_rules_and_needs_its_config() -> None:
    body = json.dumps([{"id": 1, "note": "tick 1"}, {"id": 2, "note": "tick 2"}]).encode()
    source_cfg = {"records": cfg().model_dump(mode="json")}
    fc = FilterConfig.model_validate(
        {"ignore": [{"type": "text", "pattern": "tick \\d", "pattern_kind": "regex"}]}
    )
    texts = [
        b.text
        for b in build_blocks(
            body, "application/json", "https://x/", "records", fc, HASH, None, source_cfg
        )
    ]
    assert texts == ["id: 1 | note:", "id: 2 | note:"]
    with pytest.raises(rec.RecordsError, match="needs a 'records' configuration"):
        build_blocks(body, "application/json", "https://x/", "records", FilterConfig(), HASH)
