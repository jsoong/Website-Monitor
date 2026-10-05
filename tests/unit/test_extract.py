import pytest

from pagewatch.engine.pipeline.extract import (
    decode_body,
    element_path,
    extract_blocks,
    json_blocks,
    normalize_text,
    parse_html,
    text_blocks,
)


def blocks(html: str, **kw: object) -> list[tuple[str, str]]:
    return [(b.kind, b.text) for b in extract_blocks(parse_html(html), **kw)]  # type: ignore[arg-type]


def test_block_elements_in_document_order() -> None:
    html = "<html><body><h1>Title</h1><p>One</p><ul><li>a</li><li>b</li></ul><div>tail</div></body></html>"
    assert blocks(html) == [
        ("h1", "Title"),
        ("p", "One"),
        ("li", "a"),
        ("li", "b"),
        ("div", "tail"),
    ]


def test_script_style_comments_and_head_are_dropped() -> None:
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<script>var x=1</script><!-- hidden --><noscript>enable js</noscript><p>Hi</p></body></html>"
    )
    assert blocks(html) == [("p", "Hi")]


def test_inline_elements_are_transparent_and_whitespace_collapses() -> None:
    assert blocks("<p>foo<b>bar</b>  baz\n\n  <i>qux</i></p>") == [("p", "foobar baz qux")]


def test_div_with_direct_text_and_nested_blocks_split() -> None:
    html = "<div>before<p>inner</p>after</div>"
    assert blocks(html) == [("div", "before"), ("p", "inner"), ("div", "after")]


def test_br_splits_blocks() -> None:
    assert blocks("<div>line one<br>line two<br/>line three</div>") == [
        ("div", "line one"),
        ("div", "line two"),
        ("div", "line three"),
    ]


def test_table_rows_join_cells_with_pipe() -> None:
    html = "<table><tr><th>Item</th><th>Price</th></tr><tr><td>Tea</td><td>$4.50</td></tr></table>"
    assert blocks(html) == [("tr", "Item | Price"), ("tr", "Tea | $4.50")]


def test_table_cell_with_block_children_does_not_fuse_words() -> None:
    assert blocks("<table><tr><td><p>a</p><p>b</p></td><td>c</td></tr></table>") == [
        ("tr", "a b | c")
    ]


def test_nested_list_items_become_their_own_blocks() -> None:
    html = "<ul><li>parent<ul><li>child</li></ul></li></ul>"
    assert blocks(html) == [("li", "parent"), ("li", "child")]


def test_options_dropped_by_default_and_kept_on_request() -> None:
    html = "<form><select><option>Red</option><option>Blue</option></select><p>x</p></form>"
    assert blocks(html) == [("p", "x")]
    assert blocks(html, ignore_options=False) == [("option", "Red"), ("option", "Blue"), ("p", "x")]


def test_unicode_normalization_and_invisible_characters() -> None:
    assert normalize_text("A B​C﻿ ｆｕｌｌ") == "AB C full".replace("AB C", "A BC")
    assert blocks("<p>café​­</p>") == [("p", "café")]
    assert blocks("<p>x​y</p>", nfkc=False) == [("p", "x​y")]


def test_links_and_images_resolved_against_base() -> None:
    html = '<p>See <a href="/doc">doc</a> <a href="#top">top</a><img src="pic.png"></p>'
    (b,) = extract_blocks(parse_html(html), base_url="https://example.com/dir/page")
    assert b.links == ("https://example.com/doc",)
    assert b.images == ("https://example.com/dir/pic.png",)


def test_link_inside_block_children_is_inherited() -> None:
    html = '<a href="/x"><div>click</div></a>'
    (b,) = extract_blocks(parse_html(html), base_url="https://e.com")
    assert b.text == "click" and b.links == ("https://e.com/x",)


def test_paths_match_element_path_and_index_same_tag_siblings() -> None:
    root = parse_html("<html><body><div><p>a</p><p>b</p></div><div><p>c</p></div></body></html>")
    got = extract_blocks(root)
    assert [b.path for b in got] == [
        "/html[1]/body[1]/div[1]/p[1]",
        "/html[1]/body[1]/div[1]/p[2]",
        "/html[1]/body[1]/div[2]/p[1]",
    ]
    assert element_path(root.xpath("//p")[1]) == got[1].path


def test_content_after_the_closing_html_tag_is_kept() -> None:
    html = "<html><body><p>a</p></body></html>\n<div>after div</div><p>after p</p>"
    assert blocks(html) == [("p", "a"), ("div", "after div"), ("p", "after p")]


def test_empty_and_garbage_documents() -> None:
    assert extract_blocks(parse_html("")) == []
    assert extract_blocks(parse_html("   ")) == []
    assert blocks("<p>unclosed <b>tags<div>still works") != []


@pytest.mark.parametrize(
    ("body", "ctype", "expected"),
    [
        ("héllo".encode("utf-8-sig"), "", "héllo"),
        ("héllo".encode("latin-1"), "text/html; charset=ISO-8859-1", "héllo"),
        (
            b'<meta charset="windows-1252"><p>\x93hi\x94',
            "text/html",
            '<meta charset="windows-1252"><p>“hi”',
        ),
        ("日本語のテキスト".encode("shift_jis"), "text/html", "日本語のテキスト"),
        ("héllo".encode("utf-16"), "", "héllo"),
    ],
)
def test_decode_body(body: bytes, ctype: str, expected: str) -> None:
    assert decode_body(body, ctype) == expected


def test_decode_falls_back_when_declared_charset_is_wrong() -> None:
    assert decode_body("héllo wörld".encode(), "text/html; charset=nonsense") == "héllo wörld"


def test_text_and_json_blocks() -> None:
    assert [b.text for b in text_blocks("a\n\n  b  \n")] == ["a", "b"]
    out = [b.text for b in json_blocks('{"b": 1, "a": {"x": [1, 2]}}')]
    assert out[0] == "{" and '"a": {' in out and '"b": 1' in out
    assert [b.text for b in json_blocks("not json")] == ["not json"]
