"""Import parsers and helpers (no engine needed)."""

from __future__ import annotations

import json

import pytest

from pagewatch.engine import portability as pf
from pagewatch.engine.urls import host_key, normalize_url
from tests.support.docs import make_xlsx

CHROME = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1" PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>
    <DL><p>
        <DT><A HREF="https://example.com/a" ADD_DATE="1">Alpha &amp; Omega</A>
        <DT><H3>News</H3>
        <DL><p>
            <DT><A HREF="https://news.example.com/">Daily</A>
            <DD>a description that must be ignored
        </DL><p>
        <DT><A HREF="javascript:alert(1)">bookmarklet</A>
        <DT><A HREF="place:sort=8">smart folder</A>
        <DT><A HREF="ftp://files.example.com/pub/">FTP</A>
    </DL><p>
    <DT><A HREF="https://top.example.com/">Top level</A>
</DL><p>
"""


def test_netscape_keeps_the_folder_tree_and_drops_non_pages() -> None:
    items = pf.parse_netscape(CHROME)
    got = [(i.name, i.url, i.folder) for i in items]
    assert got == [
        ("Alpha & Omega", "https://example.com/a", ["Bookmarks bar"]),
        ("Daily", "https://news.example.com/", ["Bookmarks bar", "News"]),
        ("FTP", "ftp://files.example.com/pub/", ["Bookmarks bar"]),
        ("Top level", "https://top.example.com/", []),
    ]


def test_url_list_accepts_names_comments_and_bare_hosts() -> None:
    text = """
# my list
https://example.com/a
Daily news - https://news.example.com/x
example.org/path?q=1
// another comment

not a url
"""
    items = pf.parse_urls(text)
    assert [(i.url, i.name) for i in items] == [
        ("https://example.com/a", None),
        ("https://news.example.com/x", "Daily news"),
        ("https://example.org/path?q=1", None),
        ("not a url", None),  # the importer reports it; the parser keeps the line number
    ]
    assert [i.row for i in items] == [3, 4, 5, 8]


def test_csv_columns_can_come_in_any_order_with_any_delimiter_and_a_bom() -> None:
    text = (
        "\N{ZERO WIDTH NO-BREAK SPACE}URL;Name;Folder;Interval;Keywords;Ignored\n"
        "https://a.example.com;A;News / Tech;15m;sale;x\n"
        ";;;;;\n"
        "https://b.example.com;;;2h;;\n"
        "https://c.example.com;C;;45;;\n"
    )
    items = pf.parse_csv(text)
    assert [(i.row, i.url, i.name, i.folder, i.interval_s, i.keywords, i.error) for i in items] == [
        (2, "https://a.example.com", "A", ["News", "Tech"], 900, "sale", None),
        (4, "https://b.example.com", None, [], 7200, None, None),
        (5, "https://c.example.com", "C", [], None, None, "interval '45' is under the 1 minute minimum"),
    ]  # fmt: skip


def test_csv_without_a_url_column_is_refused() -> None:
    with pytest.raises(pf.ImportError_, match="'url' column"):
        pf.parse_csv("name,address\nA,https://a.example.com\n")
    with pytest.raises(pf.ImportError_, match="empty"):
        pf.parse_csv("")


def test_xlsx_reads_the_first_sheet() -> None:
    data = make_xlsx(
        {
            "Sheet": [
                ["name", "url", "folder", "interval", "keywords"],
                ["A", "https://a.example.com", "News", "1h", "price\nsale"],
                [None, "https://b.example.com", None, 600, None],
            ],
            "Other": [["url"], ["https://ignored.example.com"]],
        }
    )
    items = pf.parse_xlsx(data)
    assert [(i.row, i.url, i.name, i.folder, i.interval_s, i.keywords) for i in items] == [
        (2, "https://a.example.com", "A", ["News"], 3600, "price\nsale"),
        (3, "https://b.example.com", None, [], 600, None),
    ]
    with pytest.raises(pf.ImportError_, match="cannot read the workbook"):
        pf.parse_xlsx(b"PK not really")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90", 90), ("90s", 90), ("15m", 900), ("2H", 7200), (" 1d ", 86400), ("1w", 604800)],
)
def test_intervals(text: str, seconds: int) -> None:
    assert pf.parse_interval(text) == seconds


@pytest.mark.parametrize("text", ["", "fast", "30", "59s", "1.5h", "-5m"])
def test_bad_intervals(text: str) -> None:
    with pytest.raises(ValueError, match="interval|minute"):
        pf.parse_interval(text)


@pytest.mark.parametrize(
    ("text", "parts"),
    [("News", ["News"]), ("News/Tech", ["News", "Tech"]), (" A > B \\ C ", ["A", "B", "C"]),
     ("", []), (None, []), ("//", [])],
)  # fmt: skip
def test_folder_paths(text: str | None, parts: list[str]) -> None:
    assert pf.folder_path(text) == parts


def test_format_detection() -> None:
    assert pf.detect_format("marks.html", None, None) == "netscape"
    assert pf.detect_format("list.CSV", None, None) == "csv"
    assert pf.detect_format("urls.txt", None, None) == "urls"
    assert pf.detect_format(None, None, b"PK\x03\x04") == "xlsx"
    assert pf.detect_format(None, CHROME, None) == "netscape"
    assert pf.detect_format(None, "name,url\nA,https://a", None) == "csv"
    assert pf.detect_format(None, '{"format": "pagewatch-export"}', None) == "json"
    assert pf.detect_format(None, "https://a.example.com", None) == "urls"


def test_a_future_export_version_is_refused_and_junk_is_explained() -> None:
    doc = {"format": "pagewatch-export", "version": 99, "exported_at": "x", "app_version": "9"}
    with pytest.raises(pf.ImportError_, match="version 99"):
        pf.parse_pagewatch(json.dumps(doc))
    with pytest.raises(pf.ImportError_, match="not a PageWatch export"):
        pf.parse_pagewatch("[1, 2]")


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("HTTP://Example.COM/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_urls_that_are_the_same_page_normalise_alike(a: str, b: str) -> None:
    assert normalize_url(a) == normalize_url(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.com/a?x=1", "https://example.com/a?x=2"),
        ("https://example.com/a", "https://www.example.com/a"),
        ("https://example.com:8443/a", "https://example.com/a"),
        ("http://example.com/a", "https://example.com/a"),
        ("https://example.com/A", "https://example.com/a"),
    ],
)
def test_urls_that_differ_stay_different(a: str, b: str) -> None:
    assert normalize_url(a) != normalize_url(b)


def test_host_key_ignores_www_and_case() -> None:
    assert host_key("https://WWW.Example.com/x") == host_key("http://example.com") == "example.com"
    assert host_key("not a url") == ""
