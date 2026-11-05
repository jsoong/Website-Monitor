"""Pure helpers of Follow-Links and merge pages."""

from __future__ import annotations

import sqlite3

import pytest

from pagewatch.engine.follow import child_defaults, compile_patterns, select_links
from pagewatch.engine.pipeline.extract import parse_html
from pagewatch.engine.pipeline.links import extract_links
from pagewatch.engine.pipeline.merge import merge_documents, next_link
from pagewatch.models import FollowLinksConfig, MergePagesConfig

HTML = "text/html; charset=utf-8"


def links(page: str, base: str = "https://site.test/a/b") -> list[tuple[str, str]]:
    return extract_links(page.encode(), HTML, base)


# -- extract_links ----------------------------------------------------------------------------


def test_links_are_absolute_deduplicated_and_in_document_order() -> None:
    page = """<body>
      <a href="/x">First</a> <a href="y">Relative</a> <a href="../up">Up</a>
      <a href="/x#frag">Same page, fragment</a> <a href="HTTPS://Other.test/Z">Abs</a>
      <a href="#top">Top</a> <a href="mailto:a@b.c">Mail</a> <a href="javascript:go()">JS</a>
      <a href="tel:+1">Tel</a> <a href="ftp://f.test/x">FTP</a> <a>no href</a> <a href="">empty</a>
    </body>"""
    assert links(page) == [
        ("https://site.test/x", "First"),
        ("https://site.test/a/y", "Relative"),
        ("https://site.test/up", "Up"),
        ("https://Other.test/Z", "Abs"),
    ]


def test_link_text_falls_back_to_title_then_image_alt_and_is_trimmed() -> None:
    page = (
        '<a href="/1"><img src="i.png" alt="Logo alt"></a>'
        '<a href="/2" title="A title"></a>'
        f'<a href="/3">{"long " * 60}</a>'
        '<a href="/4">  spaced \n out </a>'
    )
    got = dict(links(page))
    assert got["https://site.test/1"] == "Logo alt"
    assert got["https://site.test/2"] == "A title"
    assert len(got["https://site.test/3"]) == 120
    assert got["https://site.test/4"] == "spaced out"


def test_a_base_tag_changes_what_relative_links_mean() -> None:
    page = '<head><base href="https://cdn.test/dir/"></head><a href="p">P</a>'
    assert links(page) == [("https://cdn.test/dir/p", "P")]


@pytest.mark.parametrize("meta", ['<meta name="robots" content="index, NOFOLLOW">',
                                  '<meta name="pagewatch" content="nofollow">'])  # fmt: skip
def test_a_nofollow_page_has_no_links(meta: str) -> None:
    assert links(f'<head>{meta}</head><body><a href="/x">X</a></body>') == []
    assert links('<head><meta name="robots" content="noindex"></head><a href="/x">X</a>') != []


def test_empty_and_non_html_input_is_not_an_error() -> None:
    assert links("") == [] and links("   ") == []
    assert extract_links(b"%PDF-1.4 \x00\x01", "application/pdf", "https://site.test/") == []


# -- patterns and selection -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "pattern", "url", "hit"),
    [
        ("wildcard", "/news/*", "https://s.test/news/1", True),
        ("wildcard", "/news/*", "https://s.test/blog/1", False),
        ("wildcard", "*.pdf", "https://s.test/a/B.PDF", True),
        ("wildcard", "/a?c", "https://s.test/abc", True),
        ("wildcard", "/a?c", "https://s.test/ac", False),
        ("wildcard", "a.b", "https://s.test/axb", False),  # a dot is a dot, not "any character"
        ("regex", r"/news/\d+$", "https://s.test/news/12", True),
        ("regex", r"/news/\d+$", "https://s.test/news/12/x", False),
    ],
)
def test_patterns_match_anywhere_in_the_url_ignoring_case(
    kind: str, pattern: str, url: str, hit: bool
) -> None:
    (compiled,) = compile_patterns([pattern], kind)
    assert bool(compiled.search(url)) is hit


def cfg(**kw: object) -> FollowLinksConfig:
    return FollowLinksConfig.model_validate(kw)


L = [
    ("https://s.test/news/1", "One"), ("https://www.s.test/news/2", "Two"),
    ("https://s.test/about", "About"), ("https://other.test/news/3", "Other"),
    ("https://s.test/news/1/", "One again"), ("https://s.test/page", "Self"),
    ("https://s.test/news/4", "Four"),
]  # fmt: skip


def test_selection_applies_host_patterns_memory_and_self() -> None:
    got = select_links(L, cfg(include=["/news/*"]), "https://s.test/page",
                       seen={"https://s.test/news/4"}, existing=set())  # fmt: skip
    assert [u for u, _ in got] == ["https://s.test/news/1", "https://www.s.test/news/2"]
    # off-site links need same_host off; exclude beats include
    got = select_links(L, cfg(same_host=False, include=["/news/*"], exclude=["/news/2"]),
                       "https://s.test/page", seen=set(), existing={"https://s.test/news/4"})  # fmt: skip
    assert [u for u, _ in got] == ["https://s.test/news/1", "https://other.test/news/3"]
    # with no include, every same-site link qualifies (but never the page itself)
    got = select_links(L, cfg(), "https://s.test/page", seen=set(), existing=set())
    assert [t for _, t in got] == ["One", "Two", "About", "Four"]


def test_follow_and_merge_configs_are_validated() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        cfg(pattern_kind="regex", include=["("])
    with pytest.raises(ValueError, match="must not be empty"):
        cfg(exclude=[" "])
    for bad in (0, 6):
        with pytest.raises(ValueError, match="depth"):
            cfg(depth=bad)
    with pytest.raises(ValueError, match="max_pages"):
        cfg(max_pages=501)
    assert cfg().depth == 1 and cfg().max_pages == 50 and cfg().same_host  # the spec's defaults
    assert MergePagesConfig(next_selector="a.next").max_pages == 5


def test_child_defaults_are_the_parents_overrides_without_what_must_not_spread() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE b(schedule_json, fetch_json, filter_json, gate_json, actions_json, "
        "check_method, highlight_mode, priority)"
    )
    conn.execute(
        "INSERT INTO b VALUES(?,?,?,?,?,?,?,?)",
        ('{"interval_s":900}',
         '{"timeout_s":9,"follow":{"depth":2},"merge":{"next_selector":"a"},"records":{"id_field":"i"}}',
         "{}", '{"keywords":"x"}', '[{"type":"toast"}]', "static", "table", 1),
    )  # fmt: skip
    row = conn.execute("SELECT * FROM b").fetchone()
    assert child_defaults(row) == {
        "schedule": {"interval_s": 900},
        "fetch": {"timeout_s": 9},
        "gate": {"keywords": "x"},
        "actions": {"actions": [{"type": "toast"}]},
        "check_method": "static", "highlight_mode": "table", "priority": 1,
    }  # fmt: skip


# -- merge pages ------------------------------------------------------------------------------


def test_next_link_takes_the_first_match_or_the_first_link_inside_it() -> None:
    page = (
        '<a class="prev" href="/p/1">Prev</a><a class="next" href="/p/3">Next</a>'
        '<span class="pager"><a href="javascript:void(0)">x</a><a href="/p/9">Last</a></span>'
    )
    base = "https://s.test/p/2"
    args = (page.encode(), HTML, base)
    assert next_link(*args, "a.next") == "https://s.test/p/3"
    assert next_link(*args, "span.pager") == "https://s.test/p/9"  # a wrapper: its first real link
    assert next_link(*args, "a.nope") is None
    assert next_link(b'<a class="next" href="#">n</a>', HTML, base, "a.next") is None
    assert next_link(b"", HTML, base, "a.next") is None


def test_merge_appends_each_pages_body_inside_the_first_pages_body() -> None:
    first = b"<!doctype html><html><head><title>One</title></head><body><p>one</p></body></html>"
    second = b"<html><head><title>Two</title><style>x{}</style></head><body class='c'><p>two</p></body></html>"
    bare = b"<ul><li>three</li></ul>"  # a fragment (a plugin's extra) is taken whole
    body, ctype = merge_documents([(first, HTML), (second, HTML), (bare, "")])
    text = body.decode()
    assert ctype == HTML and text.count("<html") == 1 and text.count("<body") == 1
    assert text.index("one") < text.index("two") < text.index("three")
    assert '<div data-pw-page="2"><p>two</p></div>' in text
    assert "<title>Two</title>" not in text and "x{}" not in text
    root = parse_html(text)
    assert [p.text for p in root.iter("p")] == ["one", "two"]
    assert [li.text for li in root.iter("li")] == ["three"]


def test_merge_keeps_the_first_pages_dom_paths_stable() -> None:
    first = b"<html><body><div><p>one</p></div></body></html>"
    solo = merge_documents([(first, HTML)])[0].decode()
    both = merge_documents([(first, HTML), (b"<p>two</p>", HTML)])[0].decode()
    for doc in (solo, both):
        root = parse_html(doc)
        (p,) = root.xpath("/html[1]/body[1]/div[1]/p[1]")
        assert p.text == "one"


def test_merge_decodes_each_page_with_its_own_charset() -> None:
    latin = "<html><body><p>café</p></body></html>".encode("latin-1")
    utf = "<html><body><p>naïve</p></body></html>".encode()
    body, _ = merge_documents([(latin, "text/html; charset=iso-8859-1"), (utf, HTML)])
    assert "café" in body.decode() and "naïve" in body.decode()
