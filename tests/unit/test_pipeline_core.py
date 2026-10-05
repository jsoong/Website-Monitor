from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine.pipeline.core import (
    PipelineJob,
    PipelineResult,
    RebuildJob,
    VersionRef,
    ViewDiffJob,
    compute_view_diff,
    process_check,
    rebuild_version,
    resolve_kind,
)
from pagewatch.engine.pipeline.diff import DiffResult
from pagewatch.engine.store.blobs import BlobStore

PAGE = "<html><body><h1>Shop</h1><p>Tea costs $4 today</p><ul><li>one</li><li>two</li></ul></body></html>"


class Harness:
    """Drives process_check the way the runner does, keeping the version references."""

    def __init__(self, root: Path, **cfg: Any) -> None:
        self.root = root
        self.cfg = cfg
        self.latest: VersionRef | None = None
        self.anchor: VersionRef | None = None
        self.next_id = 1

    def run(self, body: str | bytes, *, ctype: str = "text/html", **over: Any) -> PipelineResult:
        data = body.encode() if isinstance(body, str) else body
        cfg = {**self.cfg, **over}
        job = PipelineJob(
            blob_root=str(self.root),
            body=data,
            content_type=ctype,
            final_url="https://example.com/",
            source_type=cfg.get("source_type", "auto"),
            filter_cfg=cfg.get("filter", {}),
            gate_cfg=cfg.get("gate", {}),
            highlight_mode=cfg.get("mode", "standard"),
            latest=self.latest,
            anchor=self.anchor,
        )
        res = process_check(job)
        if res.store_version:
            assert res.blocks_hash and res.filtered_hash
            ref = VersionRef(self.next_id, res.raw_hash, res.blocks_hash, res.filtered_hash)
            self.next_id += 1
            self.latest = ref
            if res.kind == "first" or res.alert:
                self.anchor = ref
        return res


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "blobs")


def test_first_check_stores_baseline_blobs(h: Harness) -> None:
    r = h.run(PAGE)
    assert r.kind == "first" and r.store_version and not r.alert and not r.changed
    store = BlobStore(h.root)
    assert store.get(r.raw_hash) == PAGE.encode()
    blocks = store.get_json(r.blocks_hash or "")
    assert [b["t"] for b in blocks] == ["Shop", "Tea costs $4 today", "one", "two"]
    assert r.word_count == 7 and r.summary and "Tea" in r.summary


def test_identical_raw_stops_before_parsing(h: Harness) -> None:
    h.run(PAGE)
    # a filter config that would raise if the page were parsed proves the shortcut is taken
    r = h.run(PAGE, filter={"special": {"text_only": "not-a-bool"}})
    assert r.kind == "unchanged_raw" and not r.changed and not r.store_version


def test_different_bytes_same_text_is_unchanged_and_stores_nothing(h: Harness) -> None:
    h.run(PAGE)
    before = len(list(BlobStore(h.root).iter_blobs()))
    noisy = PAGE.replace("<body>", "<body><!-- build 1234 -->\n  <script>var t=Date.now()</script>")
    r = h.run(noisy)
    assert r.kind == "unchanged" and not r.store_version
    assert len(list(BlobStore(h.root).iter_blobs())) == before


def test_real_change_alerts_with_stats_summary_and_diff_blob(h: Harness) -> None:
    first = h.run(PAGE)
    r = h.run(PAGE.replace("$4", "$3"))
    assert r.kind == "stored" and r.alert and r.changed and r.store_version
    assert r.compared_with == 1 and not r.anchor_based
    assert r.stats == {"added_words": 1, "removed_words": 1, "changed_blocks": 1}
    assert r.summary == "Tea costs $3 today"
    diff = DiffResult.from_json(BlobStore(h.root).get_json(r.diff_hash or ""))
    assert [op["t"] for op in diff.ops] == ["eq", "rep", "eq"]
    assert first.filtered_hash != r.filtered_hash


def test_below_threshold_is_stored_but_does_not_alert_and_is_not_rediffed(h: Harness) -> None:
    gate = {"min_changed_words": 5, "threshold_mode": "per_check"}
    h.run(PAGE, gate=gate)
    r = h.run(PAGE.replace("$4", "$3"), gate=gate)
    assert r.kind == "stored" and r.changed and r.store_version
    assert not r.alert and r.reason == "below_threshold" and r.diff_hash is None
    # latest advanced, so the same bytes next time are a no-op (and an old->new diff never repeats)
    assert h.run(PAGE.replace("$4", "$3"), gate=gate).kind == "unchanged_raw"


def test_cumulative_threshold_alerts_once_changes_add_up_and_reports_from_anchor(
    h: Harness,
) -> None:
    gate = {"min_changed_words": 4, "threshold_mode": "cumulative"}
    base = "<p>intro</p>"
    h.run(base, gate=gate)
    outcomes = []
    page = base
    for word in ("alpha", "beta", "gamma", "delta"):
        page += f"<p>{word}</p>"
        outcomes.append(h.run(page, gate=gate))
    assert [(o.alert, o.reason) for o in outcomes] == [
        (False, "below_threshold"),
        (False, "below_threshold"),
        (False, "below_threshold"),
        (True, None),
    ]
    last = outcomes[-1]
    assert last.anchor_based and last.compared_with == 1  # the baseline version is the anchor
    assert last.stats and last.stats["added_words"] == 4
    assert h.anchor is h.latest  # anchor moved to the alerting version


def test_ignore_removed_content(h: Harness) -> None:
    gate = {"ignore_removed": True}
    h.run(PAGE, gate=gate)
    r = h.run(PAGE.replace("<li>two</li>", ""), gate=gate)
    assert r.store_version and not r.alert and r.reason == "removed_only"
    assert h.run(PAGE.replace("<li>two</li>", "") + "<p>brand new</p>", gate=gate).alert


@pytest.mark.parametrize(
    ("gate", "reason"),
    [
        ({"min_chars": 1000}, "too_short"),
        ({"blacklist": ["tea costs"]}, "blacklist"),
        ({"whitelist": ["coffee"]}, "whitelist_miss"),
    ],
)
def test_bad_fetches_are_rejected_and_write_no_blobs(
    h: Harness, gate: dict[str, Any], reason: str
) -> None:
    r = h.run(PAGE, gate=gate)  # first check
    assert r.kind == "rejected" and r.reason == reason and not r.store_version
    assert not list(BlobStore(h.root).iter_blobs())
    assert h.latest is None  # the next check is still a "first" check


def test_bad_fetch_after_a_good_one_does_not_replace_the_baseline(h: Harness) -> None:
    gate = {"blacklist": ["service unavailable"]}
    good = h.run(PAGE, gate=gate)
    err = h.run("<html><body><h1>Service Unavailable</h1></body></html>", gate=gate)
    assert err.kind == "rejected" and not err.changed
    assert h.latest is not None and h.latest.raw_hash == good.raw_hash
    # and when the real page returns unchanged, nothing looks new
    assert h.run(PAGE, gate=gate).kind == "unchanged_raw"


def test_sort_content_makes_reordering_unchanged(h: Harness) -> None:
    flt = {"special": {"sort_content": True}}
    h.run("<p>b item</p><p>a item</p>", filter=flt)
    assert h.run("<p>a item</p><p>b item</p>", filter=flt).kind == "unchanged"


def test_reordering_is_a_move_not_a_change_in_standard_mode_but_counts_in_exact(h: Harness) -> None:
    a, b = (
        "<p>alpha one</p><p>beta two</p><p>gamma three</p>",
        "<p>beta two</p><p>gamma three</p><p>alpha one</p>",
    )
    h.run(a, gate={"min_changed_words": 1, "threshold_mode": "per_check"})
    std = h.run(b, gate={"min_changed_words": 1, "threshold_mode": "per_check"})
    assert std.changed and not std.alert and std.reason == "below_threshold"
    h2 = Harness(h.root.parent / "b2")
    h2.run(a, gate={"min_changed_words": 1, "threshold_mode": "per_check"}, mode="exact")
    exact = h2.run(b, gate={"min_changed_words": 1, "threshold_mode": "per_check"}, mode="exact")
    assert exact.alert


def test_ignore_case_default_and_off(h: Harness) -> None:
    h.run("<p>Hello World</p>")
    assert h.run("<p>HELLO WORLD</p>").kind == "unchanged"
    h2 = Harness(h.root.parent / "b2", filter={"special": {"ignore_case": False}})
    h2.run("<p>Hello World</p>")
    assert h2.run("<p>HELLO WORLD</p>").alert


def test_raw_html_mode_sees_markup_changes(h: Harness) -> None:
    flt = {"special": {"text_only": False}}
    h.run("<p class='a'>same text</p>", filter=flt)
    assert h.run("<p class='b'>same text</p>", filter=flt).alert
    h2 = Harness(h.root.parent / "b2")
    h2.run("<p class='a'>same text</p>")
    assert h2.run("<p class='b'>same text</p>").kind == "unchanged"


def test_watch_link_urls_makes_href_changes_visible(h: Harness) -> None:
    flt = {"special": {"watch_links": True}}
    h.run('<p><a href="/v1">download</a></p>', filter=flt)
    assert h.run('<p><a href="/v2">download</a></p>', filter=flt).alert


def test_json_text_and_binary_sources(h: Harness) -> None:
    r = h.run('{"stock": 5, "name": "x"}', ctype="application/json")
    assert r.kind == "first"
    changed = h.run('{"name": "x", "stock": 4}', ctype="application/json")
    assert changed.alert and changed.summary == '"stock": 4'
    t = Harness(h.root.parent / "t")
    t.run("line one\nline two", ctype="text/plain")
    assert t.run("line one\nline three", ctype="text/plain").alert
    b = Harness(h.root.parent / "b")
    assert b.run(b"\x00\x01\x02", ctype="application/octet-stream").kind == "first"
    assert b.run(b"\x00\x01\x03", ctype="application/octet-stream").alert


@pytest.mark.parametrize(
    ("source", "ctype", "body", "kind"),
    [
        ("auto", "text/html; charset=utf-8", b"x", "html"),
        ("auto", "application/json", b"{}", "json"),
        ("auto", "application/ld+json", b"{}", "json"),
        ("auto", "text/plain", b"x", "text"),
        ("auto", "", b"<!DOCTYPE html><html>", "html"),
        ("auto", "", b'{"a":1}', "json"),
        ("auto", "application/pdf", b"%PDF\x00\x01", "binary"),
        ("html", "text/plain", b"x", "html"),
        ("binary", "text/html", b"x", "binary"),
    ],
)
def test_resolve_kind(source: str, ctype: str, body: bytes, kind: str) -> None:
    assert resolve_kind(source, ctype, "https://x/", body) == kind


def test_rebuild_applies_a_new_filter_config_to_a_stored_version(h: Harness) -> None:
    r = h.run("<p>Hello World</p>", filter={"special": {"ignore_case": False}})
    job = RebuildJob(
        str(h.root),
        r.raw_hash,
        "text/html",
        "https://x/",
        "auto",
        {"special": {"sort_content": True}},
    )
    out = rebuild_version(job)
    assert out.blocks_hash and out.filtered_hash and out.word_count == 2
    assert (
        out.filtered_hash != r.filtered_hash
    )  # ignore_case default (on) lower-cases the comparison text


def test_view_diff_between_two_versions(h: Harness) -> None:
    a = h.run(PAGE)
    b = h.run(PAGE.replace("$4", "$3") + "<p>extra words here</p>")
    res = compute_view_diff(
        ViewDiffJob(str(h.root), a.blocks_hash or "", b.blocks_hash or "", True, "standard")
    )
    assert res.stats["added_words"] == 4 and not res.degraded
    assert DiffResult.from_json(BlobStore(h.root).get_json(res.diff_hash)).has_additions
