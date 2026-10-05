"""What a fetched resource *is*, and how it becomes HTML or blocks.

``resolve_kind`` decides from the bookmark's ``source_type`` first, then the content type, the
URL's extension and the file's magic bytes. Documents (PDF, DOCX, XLSX) and feeds are converted to
HTML, so everything after the parse step is the same for every source (spec: Pipeline order,
step 1). The viewer converts the same stored bytes the same way, which is what makes its
in-page highlights land on the extractor's DOM paths.
"""

from __future__ import annotations

import io
import posixpath
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from pagewatch.engine.pipeline import documents, feeds
from pagewatch.models import FeedOptions

Warn = Callable[[str], None]

#: kinds that are converted to HTML before parsing
CONVERTED_KINDS = frozenset({"pdf", "docx", "xlsx", "feed"})
_EXPLICIT = frozenset({"pdf", "docx", "xlsx", "feed", "records", "image"})
_BY_EXTENSION = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".docm": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}
_GENERIC_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/zip",
     "application/x-zip-compressed", "application/download", "application/force-download"}
)  # fmt: skip
_FEED_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+xml"}
)
_XML_TYPES = frozenset({"application/xml", "text/xml"})


def _extension(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    return posixpath.splitext(path.lower())[1]


def _xml_feed(body: bytes) -> bool:
    head = body[:2048].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


def resolve_kind(source_type: str, content_type: str, url: str, body: bytes) -> str:
    """``html`` / ``json`` / ``text`` / ``binary`` / ``image`` / ``pdf`` / ``docx`` / ``xlsx`` /
    ``feed`` / ``records``: how the bytes become blocks."""
    ct = content_type.split(";")[0].strip().lower()
    if source_type == "html":
        return "html"
    if source_type == "binary":
        return "binary"
    if source_type in _EXPLICIT:
        return source_type
    # auto, ftp, file, folder: decide from what arrived
    if ct == "application/pdf" or body[:5] == b"%PDF-":
        return "pdf"
    if "wordprocessingml" in ct:
        return "docx"
    if "spreadsheetml" in ct:
        return "xlsx"
    ext = _extension(url)
    if ext in _BY_EXTENSION and (ct in _GENERIC_TYPES or ct.startswith("application/vnd.")):
        return _BY_EXTENSION[ext]
    if ct.startswith("image/"):
        return "image"
    if ct in _FEED_TYPES or (ct in _XML_TYPES and _xml_feed(body)):
        return "feed"
    if "html" in ct or "xhtml" in ct:
        return "html"
    if ct == "application/json" or ct.endswith("+json"):
        return "json"
    if ct.startswith("text/") or ct in ("application/xml", "application/javascript"):
        return "text"
    if not ct:
        head = body[:2048].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")) or b"<body" in head:
            return "html"
        if head.startswith((b"{", b"[")):
            return "json"
    if b"\x00" in body[:4096]:
        return "binary"
    return "html" if not ct else "text"


def to_html(
    kind: str, body: bytes, source_cfg: dict[str, Any] | None = None, warn: Warn | None = None
) -> str:
    """Convert a document or feed to HTML. Raises ``documents.DocumentError``."""
    if kind == "pdf":
        return documents.pdf_to_html(body, warn)
    if kind == "docx":
        return documents.docx_to_html(body, warn)
    if kind == "xlsx":
        return documents.xlsx_to_html(body, warn)
    if kind == "feed":
        opts = FeedOptions.model_validate((source_cfg or {}).get("feed") or {})
        return feeds.feed_to_html(body, opts, warn)
    raise ValueError(f"{kind!r} is not a converted kind")


def describe_image(body: bytes, raw_hash: str) -> str:
    """A one-line description of an image resource: its format and size, then its hash."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(body)) as img:
            return (
                f"image {(img.format or 'unknown').lower()} {img.width}x{img.height} "
                f"{len(body)} bytes sha256 {raw_hash}"
            )
    except Exception:
        return f"binary content {len(body)} bytes sha256 {raw_hash}"


def records_html(raw: bytes, content_type: str, url: str, source_cfg: dict[str, Any] | None) -> str:
    """The New / Old tabs of a records source: one list item per record."""
    from html import escape

    from pagewatch.engine.pipeline import records
    from pagewatch.engine.pipeline.extract import decode_body
    from pagewatch.models import RecordsConfig

    raw_cfg = (source_cfg or {}).get("records")
    if not raw_cfg:
        return "<html><body><p>This source has no records configuration.</p></body></html>"
    cfg = RecordsConfig.model_validate(raw_cfg)
    try:
        rows = records.load_rows(decode_body(raw, content_type), cfg, content_type=content_type,
                                 url=url)  # fmt: skip
    except records.RecordsError as exc:
        note = f"These records could not be read: {escape(str(exc))}"
        return f"<html><body><p>{note}</p></body></html>"
    items = "".join(f"<li>{escape(b.text)}</li>" for b in records.record_blocks(rows, cfg))
    return f"<html><body><ul>{items}</ul></body></html>"


def view_bytes(
    raw: bytes,
    content_type: str,
    url: str,
    source_type: str,
    source_cfg: dict[str, Any] | None = None,
    *,
    plain: bool = False,
) -> tuple[bytes, str]:
    """The stored bytes as the viewer should read them: converted documents and feeds become
    HTML (with the same conversion the extractor used), everything else is returned unchanged.
    ``plain`` (the New / Old tabs) also turns a records source into a list of its records; the
    highlight view keeps it as data so it falls back to the text diff."""
    kind = resolve_kind(source_type, content_type, url, raw)
    if kind == "records" and plain:
        return records_html(raw, content_type, url, source_cfg).encode(), "text/html; charset=utf-8"
    if kind in CONVERTED_KINDS:
        try:
            return to_html(kind, raw, source_cfg).encode("utf-8"), "text/html; charset=utf-8"
        except documents.DocumentError as exc:
            note = f"<p>This {kind} could not be read: {exc}</p>"
            return f"<html><body>{note}</body></html>".encode(), "text/html; charset=utf-8"
    return raw, content_type
