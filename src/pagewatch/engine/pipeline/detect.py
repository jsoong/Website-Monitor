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


def classify(content_type: str, body: bytes, readable_chars: int) -> Kind:
    """What a fetched resource is, for the assistant's "detected type" line."""
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


def needs_browser(html_text: str, readable_chars: int) -> bool:
    """Static fetching sees (almost) nothing: under 200 readable characters, or a script-only
    app shell with an empty mount point."""
    if readable_chars >= MIN_READABLE:
        return False
    if not _SCRIPT.search(html_text) and not _NOSCRIPT_NEEDS_JS.search(html_text):
        return readable_chars < 1 and bool(html_text.strip())  # an empty page is suspicious too
    return bool(_APP_ROOT.search(html_text)) or readable_chars < MIN_READABLE
