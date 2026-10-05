from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine.pipeline import detect, viewer
from pagewatch.engine.pipeline.core import (
    RenderJob,
    ViewVersion,
    render_view,
)
from pagewatch.engine.pipeline.diff import diff_blocks
from pagewatch.engine.pipeline.extract import blocks_from_json
from pagewatch.engine.store.blobs import BlobStore
from tests.support.pipeline_harness import Harness


def highlight(old_html: str, new_html: str, tmp_path: Path, **cfg: Any) -> str:
    """Two versions through the real pipeline, then the in-page highlight of old -> new."""
    h = Harness(tmp_path / "b", **cfg)
    a = h.run(old_html)
    b = h.run(new_html)
    store = BlobStore(tmp_path / "b")
    old_texts = [x.text for x in blocks_from_json(store.get_json(a.blocks_hash or ""))]
    new_blocks = blocks_from_json(store.get_json(b.blocks_hash or ""))
    diff = diff_blocks(old_texts, [x.text for x in new_blocks])
    out = viewer.render_highlight(
        new_html.encode(), "text/html", "https://x.test/", new_blocks, old_texts, diff
    )
    assert out is not None
    return out


def squash(markup: str) -> str:
    return " ".join(markup.split())


# -- in-page highlight ------------------------------------------------------------------


def test_word_change_inside_a_paragraph_is_marked_in_place(tmp_path: Path) -> None:
    out = highlight("<p>Price $19 per box</p>", "<p>Price $17 per box</p>", tmp_path)
    assert (
        '<p class="pw-rep-block">Price $<del class="pw-del">19</del><ins class="pw-add">17</ins> per box</p>'
        in squash(out)
    )


def test_highlight_survives_inline_markup_links_and_bold(tmp_path: Path) -> None:
    old = '<p>See <a href="/offer">the <b>new</b> offer</a> today</p>'
    new = '<p>See <a href="/offer">the <b>great</b> offer</a> today</p>'
    out = squash(highlight(old, new, tmp_path))
    assert '<b><del class="pw-del">new</del><ins class="pw-add">great</ins></b>' in out
    assert '<a href="/offer"' in out  # the link survived, with a safe rel added
    # an insertion that spans two text nodes is wrapped in each
    out = squash(highlight("<p>a <i>b</i> c</p>", "<p>a <i>b X</i> Y c</p>", tmp_path))
    assert '<ins class="pw-add">X</ins>' in out and '<ins class="pw-add">Y</ins>' in out


def test_raw_whitespace_nbsp_and_entities_are_mapped_correctly(tmp_path: Path) -> None:
    old = "<p>Room  12\n   is   open &amp; ready</p>"
    new = "<p>Room  14\n   is   open &amp; ready</p>"
    out = squash(highlight(old, new, tmp_path))
    assert '<del class="pw-del">12</del><ins class="pw-add">14</ins>' in out
    assert "open &amp; ready" in out


def test_inserted_block_is_wrapped_whole_and_removed_block_shown_struck_through_in_place(
    tmp_path: Path,
) -> None:
    out = squash(
        highlight(
            "<p>keep</p><p>gone block</p><p>tail</p>",
            "<p>keep</p><p>tail</p><p>fresh block</p>",
            tmp_path,
        )
    )
    assert '<p><ins class="pw-add">fresh block</ins></p>' in out
    assert '<div class="pw-removed"><del class="pw-del">gone block</del></div><p>tail</p>' in out
    assert (
        '<aside id="pw-deleted"><h2>Removed</h2><ul><li><del class="pw-del">gone block</del></li></ul></aside>'
        in out
    )


def test_removed_rows_and_items_use_valid_containers(tmp_path: Path) -> None:
    rows = highlight(
        "<table><tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr></table>",
        "<table><tr><td>b</td><td>2</td></tr></table>",
        tmp_path,
    )
    assert (
        '<tr class="pw-removed"><td colspan="99"><del class="pw-del">a | 1</del></td></tr>'
        in squash(rows)
    )
    items = squash(highlight("<ul><li>x</li><li>y</li></ul>", "<ul><li>y</li></ul>", tmp_path))
    assert '<li class="pw-removed"><del class="pw-del">x</del></li><li>y</li>' in items


def test_removed_block_at_the_end_is_appended(tmp_path: Path) -> None:
    out = squash(highlight("<p>keep</p><p>tail gone</p>", "<p>keep</p>", tmp_path))
    assert '<div class="pw-removed"><del class="pw-del">tail gone</del></div>' in out


def test_table_row_change_marks_the_changed_cell(tmp_path: Path) -> None:
    old = "<table><tr><td>Tea</td><td>$4.50</td><td>yes</td></tr></table>"
    new = "<table><tr><td>Tea</td><td>$4.75</td><td>yes</td></tr></table>"
    out = squash(highlight(old, new, tmp_path))
    assert (
        '<tr class="pw-rep-block"><td>Tea</td><td class="pw-changed-cell">$4.75</td><td>yes</td>'
        in out
    )


def test_moved_block_is_flagged(tmp_path: Path) -> None:
    out = squash(
        highlight(
            "<p>aa bb</p><p>cc dd</p><p>ee ff</p>", "<p>cc dd</p><p>ee ff</p><p>aa bb</p>", tmp_path
        )
    )
    assert '<p class="pw-moved">aa bb</p>' in out


def test_blocks_whose_text_was_altered_by_a_filter_fall_back_to_a_whole_block_mark(
    tmp_path: Path,
) -> None:
    cfg = {
        "filter": {
            "ignore": [{"type": "text", "pattern": "Updated * ago", "pattern_kind": "wildcard"}]
        }
    }
    out = squash(
        highlight(
            "<p>Updated 5 min ago. Price $19</p>",
            "<p>Updated 9 min ago. Price $17</p>",
            tmp_path,
            **cfg,
        )
    )
    assert (
        '<p class="pw-rep-block">' in out and "Updated 9 min ago. Price $17" in out
    )  # raw text, block marked


def test_non_html_has_no_highlight_view() -> None:
    d = diff_blocks(["a"], ["b"])
    assert viewer.render_highlight(b"", "text/html", "u", [], ["a"], d) is None


# -- sanitising --------------------------------------------------------------------------

HOSTILE = (
    '<p onclick="steal()">hi <a href="javascript:alert(1)">x</a> <a href=\'https://ok.test/\'>ok</a></p>'
    "<script>alert(1)</script><iframe src='https://evil.test'></iframe><form action='/x'><input name=q></form>"
    "<img src='https://tracker.test/p.png' alt='logo' onerror='x()'><style>p{background:url(//t)}</style>"
    "<object data='x'></object><svg onload='x()'></svg><p style='background:url(//t)'>styled</p>"
)


def test_sanitizer_strips_scripts_handlers_forms_and_dangerous_urls() -> None:
    out = viewer.sanitize_fragment(HOSTILE)
    for bad in (
        "onclick",
        "onerror",
        "onload",
        "<script",
        "<iframe",
        "<form",
        "<input",
        "javascript:",
        "<style",
        "<object",
        "<svg",
        "style=",
    ):
        assert bad not in out, bad
    assert "https://ok.test/" in out and 'rel="noopener noreferrer nofollow"' in out


def test_remote_images_become_placeholders_unless_allowed(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    raw = store.put(
        b"<html><body><p>x</p><img src='https://t.test/p.png' alt='logo'></body></html>"
    )
    base = {"blob_root": str(tmp_path), "view": "new", "url": "https://x.test/", "old": None}
    off = render_view(RenderJob(new=ViewVersion(raw, "text/html", ""), **base)).html  # type: ignore[arg-type]
    assert "t.test" not in off and "[logo]" in off
    assert "img-src data:;" in off
    on = render_view(
        RenderJob(new=ViewVersion(raw, "text/html", ""), allow_remote=True, **base)
    ).html  # type: ignore[arg-type]
    assert "https://t.test/p.png" in on and "img-src data: http: https:" in on


def test_text_view_escapes_page_content() -> None:
    d = diff_blocks(["safe"], ["<script>alert(1)</script> & <b>"])
    out = viewer.render_text(d, ["safe"], ["<script>alert(1)</script> & <b>"])
    assert "<script>" not in out and "&lt;script&gt;" in out and "&amp;" in out


def test_document_wrapper_has_a_csp_and_a_body_class_for_the_deletions_toggle() -> None:
    doc = viewer.wrap_document("<p>x</p>")
    assert "Content-Security-Policy" in doc and "default-src 'none'" in doc
    assert 'class="pw-view pw-del-inline"' in doc and "<base" not in doc
    assert '<base href="https://x.test/">' in viewer.wrap_document(
        "", allow_remote=True, base_url="https://x.test/"
    )


# -- text view and render_view ----------------------------------------------------------


def test_text_view_marks_and_context_collapse() -> None:
    old = ["h", *[f"ctx{i}" for i in range(12)], "price $19"]
    new = ["h", *[f"ctx{i}" for i in range(12)], "price $17", "added line"]
    out = viewer.render_text(diff_blocks(old, new), old, new, context=2)
    assert '<ins class="pw-add">17</ins>' in out and '<del class="pw-del">19</del>' in out
    assert "unchanged blocks" in out and "ctx6" not in out


def test_render_view_end_to_end_with_cached_diff_blob(tmp_path: Path) -> None:
    h = Harness(tmp_path / "b")
    a = h.run("<p>Price $19</p><p>same</p>")
    b = h.run("<p>Price $17</p><p>same</p>")
    job = RenderJob(
        blob_root=str(tmp_path / "b"), view="highlight", url="https://x.test/",
        new=ViewVersion(b.raw_hash, "text/html", b.blocks_hash or ""),
        old=ViewVersion(a.raw_hash, "text/html", a.blocks_hash or ""), diff_hash=b.diff_hash,
    )  # fmt: skip
    res = render_view(job)
    assert res.view == "highlight" and not res.identical and res.stats["added_words"] == 1
    assert '<ins class="pw-add">17</ins>' in res.html and "<!doctype html>" in res.html
    text = render_view(replace(job, view="text"))
    assert text.view == "text" and "pw-rep" in text.html
    same = render_view(replace(job, old=job.new, diff_hash=None))
    assert same.identical and "No unread changes" in same.html
    old_view = render_view(replace(job, view="old"))
    assert "Price $19" in old_view.html and "<ins" not in old_view.html


def test_highlight_falls_back_to_the_text_view_for_non_html(tmp_path: Path) -> None:
    h = Harness(tmp_path / "b")
    a = h.run("a\nb", ctype="text/plain")
    b = h.run("a\nc", ctype="text/plain")
    res = render_view(RenderJob(
        blob_root=str(tmp_path / "b"), view="highlight", url="https://x.test/x.txt",
        new=ViewVersion(b.raw_hash, "text/plain", b.blocks_hash or ""),
        old=ViewVersion(a.raw_hash, "text/plain", a.blocks_hash or ""),
    ))  # fmt: skip
    assert res.view == "text" and "pw-rep" in res.html


# -- detection --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctype", "body", "readable", "kind"),
    [
        ("text/html", b"<html><body>" + b"word " * 100 + b"</body></html>", 500, "page"),
        (
            "text/html",
            b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>",
            0,
            "js-app",
        ),
        (
            "text/html",
            b"<html><body><div id='app'></div><script>boot()</script></body></html>",
            20,
            "js-app",
        ),
        (
            "text/html",
            b"<html><body><p>short page, no scripts at all</p></body></html>",
            30,
            "page",
        ),
        ("application/rss+xml", b"<rss></rss>", 0, "feed"),
        ("text/xml", b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>", 0, "feed"),
        ("application/pdf", b"%PDF-1.7", 0, "pdf"),
        ("application/json", b"{}", 2, "json"),
        ("text/plain", b"hi", 2, "text"),
        ("image/png", b"\x89PNG", 0, "image"),
        ("application/octet-stream", b"\x00\x01", 0, "binary"),
    ],
)
def test_classify(ctype: str, body: bytes, readable: int, kind: str) -> None:
    assert detect.classify(ctype, body, readable) == kind
