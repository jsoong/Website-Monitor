"""Source kind resolution, view conversion, auto-detection, dispatch and the new models."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from pagewatch.engine.fetch.select import FetcherSet, Route, method_kind, route_for
from pagewatch.engine.pipeline import detect, sources
from pagewatch.models import (
    BookmarkIn,
    BrowserOptions,
    CheckKind,
    FeedOptions,
    FetchConfig,
    FilterConfig,
    Rect,
    ScreenshotFilters,
    Settings,
)
from tests.support.docs import make_pdf
from tests.support.fakes import ScriptedFetcher, ok

PDF = make_pdf([["x"]])
RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
PNG = io.BytesIO()
Image.new("RGB", (4, 3), "red").save(PNG, "PNG")


@pytest.mark.parametrize(
    ("source", "ctype", "url", "body", "kind"),
    [
        ("auto", "application/pdf", "https://x/a", b"", "pdf"),
        ("auto", "application/octet-stream", "https://x/report.PDF?dl=1", PDF, "pdf"),
        ("auto", "", "https://x/a", PDF, "pdf"),  # magic bytes
        ("auto", "application/octet-stream", "https://x/a.docx", b"PK\x03\x04", "docx"),
        ("auto", "application/zip", "https://x/a.xlsx", b"PK\x03\x04", "xlsx"),
        ("auto", "application/vnd.ms-excel.sheet.macroenabled.12", "https://x/a.xlsm", b"", "xlsx"),
        (
            "auto",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://x/doc",
            b"PK",
            "docx",
        ),
        ("auto", "application/rss+xml", "https://x/f", RSS, "feed"),
        ("auto", "application/atom+xml; charset=utf-8", "https://x/f", b"<feed/>", "feed"),
        ("auto", "application/xml", "https://x/f", RSS, "feed"),
        ("auto", "text/xml", "https://x/sitemap", b"<urlset/>", "text"),
        ("auto", "image/png", "https://x/i.png", PNG.getvalue(), "image"),
        ("auto", "text/html", "https://x/", b"<p>x</p>", "html"),
        ("auto", "application/json", "https://x/api", b"{}", "json"),
        ("auto", "text/csv", "https://x/d.csv", b"a,b", "text"),
        ("ftp", "application/pdf", "ftp://h/a.pdf", PDF, "pdf"),
        ("file", "text/plain", "file:///a.txt", b"hi", "text"),
        ("folder", "text/html; charset=utf-8", "file:///d", b"<table></table>", "html"),
        # an explicit type wins over what the server claims
        ("pdf", "text/html", "https://x/a", b"", "pdf"),
        ("docx", "application/octet-stream", "https://x/a", b"", "docx"),
        ("xlsx", "text/plain", "https://x/a", b"", "xlsx"),
        ("feed", "text/html", "https://x/a", b"", "feed"),
        ("records", "text/html", "https://x/a", b"", "records"),
        ("image", "application/octet-stream", "https://x/a", b"", "image"),
        ("html", "application/pdf", "https://x/a", PDF, "html"),
        ("binary", "text/html", "https://x/a", b"x", "binary"),
    ],
)
def test_resolve_kind(source: str, ctype: str, url: str, body: bytes, kind: str) -> None:
    assert sources.resolve_kind(source, ctype, url, body) == kind


def test_image_description_has_format_size_and_hash() -> None:
    text = sources.describe_image(PNG.getvalue(), "ab" * 32)
    assert text.startswith("image png 4x3 ") and text.endswith("ab" * 32)
    assert sources.describe_image(b"not an image", "cd" * 32).startswith("binary content 12 bytes")


def test_view_bytes_converts_documents_and_feeds_but_not_html_or_json() -> None:
    raw, ctype = sources.view_bytes(PDF, "application/pdf", "https://x/a", "auto")
    assert ctype.startswith("text/html") and b"<p>x</p>" in raw
    raw, ctype = sources.view_bytes(RSS, "application/rss+xml", "https://x/a", "auto")
    assert ctype.startswith("text/html") and b"<ul>" in raw
    same, ctype = sources.view_bytes(b"<p>a</p>", "text/html", "https://x/a", "auto")
    assert (same, ctype) == (b"<p>a</p>", "text/html")


def test_view_bytes_turns_an_unreadable_document_into_a_note_not_an_error() -> None:
    raw, ctype = sources.view_bytes(b"junk", "application/pdf", "https://x/a", "pdf")
    assert b"could not be read" in raw and ctype.startswith("text/html")


def test_view_bytes_lists_records_only_for_the_plain_tabs() -> None:
    body = json.dumps([{"id": 2, "n": "b"}, {"id": 1, "n": "a"}]).encode()
    cfg = {"records": {"id_field": "id"}}
    raw, ctype = sources.view_bytes(
        body, "application/json", "https://x/a", "records", cfg, plain=True
    )
    assert ctype.startswith("text/html") and raw.index(b"id: 1") < raw.index(b"id: 2")
    kept, ctype = sources.view_bytes(body, "application/json", "https://x/a", "records", cfg)
    assert kept == body and ctype == "application/json"  # highlight falls back to the text diff
    note, _ = sources.view_bytes(body, "application/json", "https://x/a", "records", {}, plain=True)
    assert b"no records configuration" in note


# -- auto-detection ---------------------------------------------------------------------

SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
SMALL_INLINE = "<html><body><p>No openings</p><script>var build=3</script></body></html>"
BIG_INLINE = f"<html><body><p>Hi</p><script>{'x=1;' * 800}</script></body></html>"
NOSCRIPT = (
    "<html><body><noscript>You need to enable JavaScript to run this app.</noscript></body></html>"
)
EXTERNAL = '<html><body><p>Loading…</p><script src="/bundle.js"></script></body></html>'
STATIC_SHORT = "<html><body><p>No openings right now.</p></body></html>"


@pytest.mark.parametrize(
    ("html", "chars", "reason"),
    [
        (SHELL, 0, "app_shell"),
        (EXTERNAL, 8, "little_text:8"),
        (BIG_INLINE, 2, "little_text:2"),
        (NOSCRIPT, 0, "little_text:0"),
        (SMALL_INLINE, 11, None),  # a few bytes of inline script do not make an app
        (STATIC_SHORT, 24, None),  # a short page is just a short page
        (EXTERNAL, 5000, None),  # plenty of text: static is fine
        ("<html><body></body></html>", 0, "empty_page"),
        ("", 0, None),
    ],
)
def test_browser_reason(html: str, chars: int, reason: str | None) -> None:
    assert detect.browser_reason(html, chars) == reason
    assert detect.needs_browser(html, chars) == (reason is not None)


def test_classify_prefers_the_resolved_kind() -> None:
    assert detect.classify("application/octet-stream", b"", 0, "pdf") == "pdf"
    assert detect.classify("text/html", b"", 0, "records") == "json"
    assert detect.classify("text/html", SHELL.encode(), 0, "html") == "js-app"
    assert detect.classify("text/html", b"<p>x</p>", 500, None) == "page"


# -- dispatch ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "source", "method", "route", "kind"),
    [
        ("https://x/", "auto", "auto", "static", CheckKind.STATIC),
        ("http://x/", "html", "static", "static", CheckKind.STATIC),
        ("https://x/", "auto", "browser", "browser", CheckKind.BROWSER),
        ("https://x/", "auto", "screenshot", "screenshot", CheckKind.SCREENSHOT),
        ("https://x/a.pdf", "pdf", "auto", "static", CheckKind.STATIC),
        ("ftp://h/f", "auto", "auto", "ftp", CheckKind.FTP),
        ("FTPS://h/f", "ftp", "static", "ftp", CheckKind.FTP),
        ("file:///tmp/a", "file", "auto", "file", CheckKind.FILE),
    ],
)
def test_route_for(url: str, source: str, method: str, route: str, kind: CheckKind) -> None:
    got = route_for(url, source, method)
    assert got is not None and (got.name, got.kind) == (route, kind)


def test_route_for_unsupported_and_feed_enclosures() -> None:
    assert route_for("gopher://x/", "auto", "auto") is None
    assert route_for("not a url", "auto", "auto") is None
    plain = FetchConfig()
    assert route_for("https://x/f", "feed", "auto", plain).name == "static"  # type: ignore[union-attr]
    keep = FetchConfig(feed=FeedOptions(download_enclosures=True, enclosures_dir="/tmp/e"))
    assert route_for("https://x/f", "feed", "auto", keep).name == "feed"  # type: ignore[union-attr]
    assert route_for("https://x/f", "auto", "auto", keep).name == "static"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("route", "content", "expected"),
    [
        (Route("static", CheckKind.STATIC), "html", CheckKind.STATIC),
        (Route("static", CheckKind.STATIC), "pdf", CheckKind.DOCUMENT),
        (Route("static", CheckKind.STATIC), "docx", CheckKind.DOCUMENT),
        (Route("static", CheckKind.STATIC), "xlsx", CheckKind.DOCUMENT),
        (Route("static", CheckKind.STATIC), "feed", CheckKind.FEED),
        (Route("static", CheckKind.STATIC), "records", CheckKind.RECORDS),
        (Route("static", CheckKind.STATIC), None, CheckKind.STATIC),
        (Route("browser", CheckKind.BROWSER), "pdf", CheckKind.BROWSER),
        (Route("screenshot", CheckKind.SCREENSHOT), "html", CheckKind.SCREENSHOT),
        (Route("ftp", CheckKind.FTP), "pdf", CheckKind.FTP),
        (Route("file", CheckKind.FILE), "records", CheckKind.FILE),
    ],
)
def test_method_kind_names_the_check_after_the_content(
    route: Route, content: str | None, expected: CheckKind
) -> None:
    assert method_kind(route, content) is expected


async def test_fetcher_set_dispatches_replaces_and_closes() -> None:
    a = ScriptedFetcher(lambda r, n: ok(r, "a"))
    b = ScriptedFetcher(lambda r, n: ok(r, "b"))
    fs = FetcherSet({"static": a, "browser": b})  # type: ignore[dict-item]
    assert fs.get(Route("static", CheckKind.STATIC)) is a
    fs.replace("browser", a)  # type: ignore[arg-type]
    assert fs.get(Route("browser", CheckKind.BROWSER)) is a
    await fs.aclose()


# -- models -----------------------------------------------------------------------------


def test_new_fetch_options_have_the_specified_defaults() -> None:
    fc = FetchConfig()
    assert fc.browser == BrowserOptions() and fc.browser.scroll_count == 0
    assert fc.browser.full_page is True and fc.browser.clip is None
    assert fc.feed.max_entries == 200 and fc.listing.recursive is False and fc.records is None
    s = FilterConfig().screenshot
    assert s.min_ratio == pytest.approx(0.002) and s.height_change_pct == 5.0 and s.ignore == []
    st = Settings()
    assert st.browser_channel == "msedge" and st.browser_executable is None


@pytest.mark.parametrize(
    "bad",
    [
        {"browser": {"scroll_count": 51}},
        {"browser": {"delay_after_load_s": -1}},
        {"browser": {"keys": ["a"] * 11}},
        {"browser": {"clip": {"x": 0, "y": 0, "w": 0, "h": 5}}},
        {"feed": {"download_enclosures": True}},
        {"feed": {"max_entries": 0}},
        {"listing": {"max_entries": 0}},
        {"records": {"path": "$", "id_field": ""}},
        {"records": {"id_field": "id", "format": "xml"}},
        {"browser": {"nope": 1}},
    ],
)
def test_fetch_option_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FetchConfig.model_validate(bad)


def test_screenshot_filter_validation() -> None:
    assert ScreenshotFilters(ignore=[Rect(x=1, y=2, w=3, h=4)]).ignore[0].w == 3
    with pytest.raises(ValidationError):
        ScreenshotFilters(min_ratio=1.5)
    with pytest.raises(ValidationError):
        Rect(x=-1, y=0, w=1, h=1)


def test_a_records_url_can_be_any_supported_scheme() -> None:
    for url in ("https://x/a.json", "file:///data/a.csv", "ftp://h/a.csv"):
        assert BookmarkIn(url=url, source_type="records").url == url  # type: ignore[arg-type]
