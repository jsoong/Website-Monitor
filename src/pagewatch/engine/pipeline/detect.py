"""Content classification for the add-bookmark assistant and method auto-detection."""

from __future__ import annotations

import re
from typing import Literal

Kind = Literal["page", "js-app", "feed", "pdf", "docx", "xlsx", "json", "text", "image", "binary"]

_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)
_APP_ROOT = re.compile(
    r"""<div[^>]+id\s*=\s*["'](?:root|app|__next|__nuxt|svelte|ember\d*)["'][^>]*>\s*</div>""",
    re.IGNORECASE,
)
_NOSCRIPT_NEEDS_JS = re.compile(
    r"enable javascript|requires javascript|javascript is required", re.I
)
MIN_READABLE = 200


_RESOLVED: dict[str, Kind] = {
    "pdf": "pdf", "docx": "docx", "xlsx": "xlsx", "feed": "feed", "image": "image",
    "binary": "binary", "records": "json",
}  # fmt: skip


def classify(
    content_type: str, body: bytes, readable_chars: int, resolved: str | None = None
) -> Kind:
    """What a fetched resource is, for the assistant's "detected type" line. ``resolved`` is
    ``sources.resolve_kind``'s answer (it knows the URL extension and the bookmark's type)."""
    if resolved in _RESOLVED:
        return _RESOLVED[resolved]
    ct = content_type.split(";")[0].strip().lower()
    head = body[:2048].lstrip().lower()
    if ct in ("application/rss+xml", "application/atom+xml", "application/feed+json") or (
        ct in ("application/xml", "text/xml") and (b"<rss" in head or b"<feed" in head)
    ):
        return "feed"
    if ct == "application/pdf" or body.startswith(b"%PDF"):
        return "pdf"
    if "wordprocessingml" in ct or ct == "application/msword":
        return "docx"
    if "spreadsheetml" in ct or ct == "application/vnd.ms-excel":
        return "xlsx"
    if ct.startswith("image/"):
        return "image"
    if ct == "application/json" or ct.endswith("+json"):
        return "json"
    if ct.startswith("text/plain"):
        return "text"
    if b"\x00" in body[:4096] and "html" not in ct:
        return "binary"
    if needs_browser(body.decode("utf-8", errors="ignore"), readable_chars):
        return "js-app"
    return "page"


_EXTERNAL_SCRIPT = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_INLINE_SCRIPT = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.IGNORECASE | re.S
)
BIG_INLINE_SCRIPT = 2000  # bytes of inline script that make a short page "an app"


def browser_reason(html_text: str, readable_chars: int) -> str | None:
    """Why a static fetch shows (almost) nothing and the page needs a browser, or ``None``.

    Under 200 readable characters is the trigger (spec), but only on a page whose content can
    come from scripts: an empty app mount point (``app_shell``), a "requires JavaScript"
    notice, an external script or a large inline one (``little_text:<n>``). A short page whose
    only script is a few bytes of inline code is just a short page ("No openings"), and a browser
    costs one of the three scarce pool slots. ``empty_page``: a non-empty document with no text."""
    if readable_chars >= MIN_READABLE:
        return None
    if not _SCRIPT.search(html_text) and not _NOSCRIPT_NEEDS_JS.search(html_text):
        return "empty_page" if readable_chars < 1 and html_text.strip() else None
    if _APP_ROOT.search(html_text):
        return "app_shell"
    inline = sum(len(m) for m in _INLINE_SCRIPT.findall(html_text))
    if (
        _EXTERNAL_SCRIPT.search(html_text)
        or _NOSCRIPT_NEEDS_JS.search(html_text)
        or inline >= BIG_INLINE_SCRIPT
    ):
        return f"little_text:{readable_chars}"
    return "empty_page" if readable_chars < 1 and html_text.strip() else None


def needs_browser(html_text: str, readable_chars: int) -> bool:
    """Static fetching sees (almost) nothing: see ``browser_reason``."""
    return browser_reason(html_text, readable_chars) is not None
