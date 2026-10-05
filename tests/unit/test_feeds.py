"""RSS and Atom to blocks: one per entry, keyed by id or link (spec: Fetch layer, feed row)."""

from __future__ import annotations

import pytest

from pagewatch.engine.fetch.feed import enclosure_name
from pagewatch.engine.pipeline import feeds
from pagewatch.engine.pipeline.core import build_blocks
from pagewatch.engine.pipeline.documents import DocumentError
from pagewatch.models import FeedOptions, FilterConfig

HASH = "0" * 64


def rss(*items: tuple[str, str, str], title: str = "Town News") -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>https://town.test/{slug}</link>"
        f"<guid>{slug}</guid><description>{d}</description>"
        f"<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>"
        for t, slug, d in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>{title}</title><link>https://town.test/</link><description>d</description>"
        f"{body}</channel></rss>"
    ).encode()


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Releases</title><id>urn:x</id>
<updated>2026-10-05T10:00:00Z</updated>
<entry><title>v2.0</title><id>urn:v2</id><link href="https://r.test/v2"/>
<updated>2026-10-05T10:00:00Z</updated><summary>Big release</summary></entry>
<entry><title>v1.9</title><id>urn:v19</id><link href="https://r.test/v19"/>
<updated>2026-09-01T10:00:00Z</updated><content type="html">&lt;p&gt;Bug &lt;b&gt;fixes&lt;/b&gt;&lt;/p&gt;</content></entry>
</feed>"""


def blocks(body: bytes, ctype: str = "application/rss+xml", source: str = "auto") -> list[str]:
    cfg = FilterConfig()
    return [b.text for b in build_blocks(body, ctype, "https://town.test/feed", source, cfg, HASH)]


def test_rss_one_block_per_entry_with_title_and_summary() -> None:
    body = rss(("Council meets", "a", "Budget vote Tuesday"), ("Road closed", "b", "Main St"))
    assert blocks(body) == [
        "Town News",
        "Council meets — Budget vote Tuesday",
        "Road closed — Main St",
    ]


def test_atom_summary_or_html_content_becomes_plain_text() -> None:
    assert blocks(ATOM, "application/atom+xml") == [
        "Releases",
        "v2.0 — Big release",
        "v1.9 — Bug fixes",
    ]


def test_a_new_entry_is_one_inserted_block_and_timestamps_are_not_part_of_the_text() -> None:
    a = blocks(rss(("Old", "a", "x")))
    b = blocks(rss(("New", "n", "fresh"), ("Old", "a", "x")))
    assert b[:-1] == [a[0], "New — fresh"] and b[-1] == a[-1]
    assert not any("2026" in t for t in b)


def test_duplicate_entry_ids_are_kept_once() -> None:
    body = rss(("Same", "dup", "one"), ("Same again", "dup", "two"))
    assert blocks(body) == ["Town News", "Same — one"]


def test_max_entries_and_summary_options() -> None:
    body = rss(("A", "a", "sa"), ("B", "b", "sb"), ("C", "c", "sc"))
    warnings: list[str] = []
    html = feeds.feed_to_html(body, FeedOptions(max_entries=2, summary=False), warnings.append)
    assert html.count("<li") == 2 and "sa" not in html and any("2 entries" in w for w in warnings)


def test_xml_content_type_is_recognised_as_a_feed_by_its_root_element() -> None:
    assert blocks(rss(("T", "t", "d")), "application/xml")[1] == "T — d"
    assert blocks(rss(("T", "t", "d")), "text/xml")[1] == "T — d"


def test_explicit_feed_source_type_with_a_generic_content_type() -> None:
    assert blocks(rss(("T", "t", "d")), "application/octet-stream", "feed")[1] == "T — d"


@pytest.mark.parametrize("body", [b"<html><body>hi</body></html>", b"plain words", b""])
def test_not_a_feed_is_a_document_error(body: bytes) -> None:
    with pytest.raises(DocumentError, match="not an RSS or Atom feed"):
        build_blocks(body, "text/plain", "https://x/", "feed", FilterConfig(), HASH)


def test_entry_markup_is_escaped_in_the_generated_html() -> None:
    html = feeds.feed_to_html(rss(("<b>x</b> & y", "e", "<script>1</script>t")), FeedOptions())
    assert "<script>" not in html


def test_enclosure_file_names_are_stable_and_safe() -> None:
    a = enclosure_name("https://pod.test/ep/1.mp3?x=1")
    assert a == enclosure_name("https://pod.test/ep/1.mp3?x=1") and a.endswith("-1.mp3")
    assert "/" not in enclosure_name("https://pod.test/a/../../etc/passwd")
    assert enclosure_name("https://pod.test/") != enclosure_name("https://pod.test/x")
