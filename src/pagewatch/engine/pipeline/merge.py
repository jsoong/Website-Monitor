"""Merge pages (spec: Multi-page sources): find the next page of a listing and concatenate the
pages before extraction.

Both functions run in a worker process (they parse HTML); the runner does the fetching between
them. Each merged page is wrapped in ``<div data-pw-page="N">`` and appended inside the first
page's ``<body>``, so the blocks of page 1 keep their DOM paths and the pages stay separate.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from pagewatch.engine.pipeline import filters
from pagewatch.engine.pipeline.extract import decode_body, parse_html

_BODY = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.IGNORECASE | re.DOTALL)
_BODY_OPEN = re.compile(r"<body\b[^>]*>(.*)", re.IGNORECASE | re.DOTALL)
_CLOSERS = re.compile(r"</\s*(?:body|html)\s*>\s*$", re.IGNORECASE)
_HEAD = re.compile(
    r"<head\b.*?</head\s*>|<!doctype[^>]*>|</?html\b[^>]*>", re.IGNORECASE | re.DOTALL
)
UTF8_HTML = "text/html; charset=utf-8"


def next_link(body: bytes, content_type: str, base_url: str, selector: str) -> str | None:
    """The absolute URL of the "next page" link: the ``href`` of the first element ``selector``
    matches, or, when that element is a wrapper, of the first usable link inside it."""
    root = parse_html(decode_body(body, content_type))
    if root is None:
        return None
    try:
        found = filters.select(root, "css", selector)
    except Exception:
        return None
    for el in found:
        hrefs = [el.get("href"), *(a.get("href") for a in el.iter("a") if a is not el)]
        for raw in hrefs:
            href = str(raw or "").strip()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                return urljoin(base_url, href)
    return None


def _inner(markup: str) -> str:
    """What a page contributes when it is appended to another: its body, or all of it."""
    m = _BODY.search(markup) or _BODY_OPEN.search(markup)
    if m:
        return m.group(1)
    return _HEAD.sub("", markup)


def merge_documents(parts: list[tuple[bytes, str]]) -> tuple[bytes, str]:
    """Concatenate pages: the first page whole, the others appended inside its ``<body>``."""
    texts = [decode_body(body, ctype or UTF8_HTML) for body, ctype in parts]
    merged = _CLOSERS.sub("", _CLOSERS.sub("", texts[0].rstrip()))
    pieces = [merged]
    for number, text in enumerate(texts[1:], 2):
        pieces.append(f'\n<div data-pw-page="{number}">{_inner(text)}</div>')
    pieces.append("\n</body></html>")
    return "".join(pieces).encode("utf-8"), UTF8_HTML
