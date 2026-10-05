"""Bytes -> text -> ordered list of text blocks.

A *block* is one block-level element's own text (``p``, ``li``, ``tr``, ``h1``..``h6``,
``pre``, ``blockquote``, a ``div`` with direct text...). Nested block elements become their
own blocks, so a wrapper element that only gains or loses a ``div`` does not disturb the
sequence. Script, style, noscript and comments are dropped, whitespace collapses, and table
cells join with `` | ``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from charset_normalizer import from_bytes
from lxml import etree

# Block-level containers. ``tr`` is handled separately (one block per row).
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption", "dd", "details", "div",
        "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hgroup", "hr", "html", "li", "main", "menu", "nav", "ol",
        "p", "pre", "section", "summary", "table", "tbody", "tfoot", "thead", "ul",
        "select", "option", "optgroup", "datalist",
    }
)  # fmt: skip
DROP_TAGS = frozenset(
    {
        "script", "style", "noscript", "template", "head", "svg", "iframe", "object", "embed",
        "canvas", "textarea",
    }
)  # fmt: skip
OPTION_TAGS = frozenset({"option", "datalist", "optgroup"})

_WS = re.compile(r"\s+")
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁯﻿]")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-:.]+)", re.I)
_XML_DECL = re.compile(rb"""^\s*<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9_\-:.]+)""", re.I)


@dataclass(slots=True)
class Block:
    path: str  # DOM path of the element the text came from, e.g. /html[1]/body[1]/div[2]/p[1]
    kind: str  # tag name, or "line" / "link" / "image" for synthetic blocks
    text: str
    links: tuple[str, ...] = ()
    images: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"p": self.path, "k": self.kind, "t": self.text}
        if self.links:
            out["l"] = list(self.links)
        if self.images:
            out["i"] = list(self.images)
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Block:
        return cls(d["p"], d["k"], d["t"], tuple(d.get("l", ())), tuple(d.get("i", ())))


def blocks_to_json(blocks: list[Block]) -> list[dict[str, Any]]:
    return [b.to_json() for b in blocks]


def blocks_from_json(data: list[dict[str, Any]]) -> list[Block]:
    return [Block.from_json(d) for d in data]


# -- decoding ---------------------------------------------------------------------------


def decode_body(body: bytes, content_type: str = "") -> str:
    """Charset from the BOM, then the HTTP header, then ``<meta>``, then detection."""
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", errors="replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16", errors="replace")
    candidates: list[str] = []
    m = _HEADER_CHARSET.search(content_type or "")
    if m:
        candidates.append(m.group(1))
    head = body[:4096]
    for rx in (_META_CHARSET, _XML_DECL):
        m2 = rx.search(head)
        if m2:
            candidates.append(m2.group(1).decode("ascii", errors="ignore"))
    for name in candidates:
        try:
            return body.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best = from_bytes(body[: 256 * 1024]).best()
    if best is not None:
        try:
            return body.decode(best.encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return body.decode("latin-1")


def normalize_text(text: str, *, nfkc: bool = True) -> str:
    if nfkc:
        text = unicodedata.normalize("NFKC", text)
        text = _INVISIBLE.sub("", text)
    return _WS.sub(" ", text).strip()


# -- HTML -------------------------------------------------------------------------------


_CLOSERS = re.compile(r"</\s*(?:body|html)\s*>", re.IGNORECASE)


def parse_html(text: str) -> Any | None:
    """Parse leniently; returns the root element or ``None`` for an empty document.

    libxml2 silently discards anything after ``</html>``, which for a change monitor would
    hide real changes (some sites append widgets or a whole section there). Browsers fold
    such content into the body, so the closing tags are dropped before parsing.
    """
    if not text.strip():
        return None
    text = _CLOSERS.sub("", text)
    parser = etree.HTMLParser(
        encoding="utf-8", remove_comments=True, remove_pis=True, recover=True, no_network=True
    )
    try:
        return etree.fromstring(text.encode("utf-8", errors="replace"), parser)
    except etree.XMLSyntaxError:
        return None


class _Ctx:
    __slots__ = ("images", "kind", "links", "parts", "path")

    def __init__(self, path: str, kind: str, links: list[str]) -> None:
        self.path = path
        self.kind = kind
        self.parts: list[str] = []
        self.links = links
        self.images: list[str] = []


class _Extractor:
    def __init__(self, base_url: str, *, ignore_options: bool, nfkc: bool) -> None:
        self.base_url = base_url
        self.ignore_options = ignore_options
        self.nfkc = nfkc
        self.blocks: list[Block] = []
        self._hrefs: list[str] = []

    # emit -----------------------------------------------------------------------------

    def _flush(self, ctx: _Ctx) -> None:
        if ctx.parts:
            text = normalize_text("".join(ctx.parts), nfkc=self.nfkc)
            if text:
                self.blocks.append(
                    Block(ctx.path, ctx.kind, text, _dedupe(ctx.links), _dedupe(ctx.images))
                )
        ctx.parts.clear()
        ctx.links = list(self._hrefs)
        ctx.images = []

    def _new(self, path: str, kind: str) -> _Ctx:
        return _Ctx(path, kind, list(self._hrefs))

    # walking --------------------------------------------------------------------------

    def run(self, root: Any, path: str, *, as_block: bool) -> list[Block]:
        tag = root.tag if isinstance(root.tag, str) else "div"
        if tag == "tr":
            self._row(root, path)
            return self.blocks
        ctx = self._new(path, tag)
        if as_block or tag in BLOCK_TAGS:
            self._add(ctx, root.text)
            self._children(root, path, ctx)
            self._flush(ctx)
        else:  # an inline element chosen as a watch region: treat it as one block
            self._element(root, path, ctx)
            self._flush(ctx)
        return self.blocks

    def _add(self, ctx: _Ctx, text: str | None) -> None:
        if text:
            ctx.parts.append(text)

    def _children(self, el: Any, path: str, ctx: _Ctx) -> None:
        counters: dict[str, int] = {}
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):  # comment, processing instruction, entity
                self._add(ctx, child.tail)
                continue
            counters[tag] = counters.get(tag, 0) + 1
            self._element(child, f"{path}/{tag}[{counters[tag]}]", ctx)
            self._add(ctx, child.tail)

    def _element(self, el: Any, path: str, ctx: _Ctx) -> None:
        tag: str = el.tag
        if tag in DROP_TAGS or (self.ignore_options and tag in OPTION_TAGS):
            return
        if tag == "br" or tag == "hr":
            self._flush(ctx)
            return
        if tag == "tr":
            self._flush(ctx)
            self._row(el, path)
            return
        if tag in BLOCK_TAGS:
            self._flush(ctx)
            inner = self._new(path, tag)
            self._add(inner, el.text)
            self._children(el, path, inner)
            self._flush(inner)
            return
        # inline element: transparent
        pushed = False
        if tag == "a":
            href = _clean_url(el.get("href"), self.base_url)
            if href:
                self._hrefs.append(href)
                ctx.links.append(href)
                pushed = True
        elif tag == "img":
            src = _clean_url(el.get("src") or el.get("data-src"), self.base_url)
            if src:
                ctx.images.append(src)
        self._add(ctx, el.text)
        self._children(el, path, ctx)
        if pushed:
            self._hrefs.pop()

    def _row(self, tr: Any, path: str) -> None:
        cells = [c for c in tr if isinstance(c.tag, str) and c.tag in ("td", "th")]
        links: list[str] = []
        images: list[str] = []
        texts: list[str] = []
        if not cells:
            parts: list[str] = []
            self._collect(tr, parts, links, images)
            texts.append(normalize_text("".join(parts), nfkc=self.nfkc))
        for cell in cells:
            parts = []
            self._collect(cell, parts, links, images)
            texts.append(normalize_text("".join(parts), nfkc=self.nfkc))
        if any(texts):
            self.blocks.append(
                Block(path, "tr", " | ".join(texts), _dedupe(links), _dedupe(images))
            )

    def _collect(self, el: Any, out: list[str], links: list[str], images: list[str]) -> None:
        """Flatten an element's text; block-level descendants are separated by a space."""
        if el.text:
            out.append(el.text)
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):
                if child.tail:
                    out.append(child.tail)
                continue
            if tag in DROP_TAGS or (self.ignore_options and tag in OPTION_TAGS):
                pass
            elif tag == "br" or tag in BLOCK_TAGS or tag == "tr":
                out.append(" ")
                self._collect(child, out, links, images)
                out.append(" ")
            else:
                if tag == "a":
                    href = _clean_url(child.get("href"), self.base_url)
                    if href:
                        links.append(href)
                elif tag == "img":
                    src = _clean_url(child.get("src") or child.get("data-src"), self.base_url)
                    if src:
                        images.append(src)
                self._collect(child, out, links, images)
            if child.tail:
                out.append(child.tail)


def _clean_url(raw: str | None, base: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    try:
        return urljoin(base, raw) if base else raw
    except ValueError:
        return None


def _dedupe(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def element_path(el: Any) -> str:
    """The same ``/html[1]/body[1]/div[2]`` path ``extract_blocks`` assigns to blocks."""
    parts: list[str] = []
    node = el
    while node is not None:
        tag = node.tag
        if not isinstance(tag, str):
            return ""
        parent = node.getparent()
        if parent is None:
            parts.append(f"{tag}[1]")
        else:
            idx = 1
            for sib in node.itersiblings(preceding=True):
                if sib.tag == tag:
                    idx += 1
            parts.append(f"{tag}[{idx}]")
        node = parent
    return "/" + "/".join(reversed(parts))


def extract_blocks(
    root: Any,
    *,
    base_url: str = "",
    ignore_options: bool = True,
    nfkc: bool = True,
    path: str | None = None,
    as_block: bool = False,
) -> list[Block]:
    """Extract the blocks of ``root`` (the document, or a watched sub-tree)."""
    if root is None:
        return []
    ex = _Extractor(base_url, ignore_options=ignore_options, nfkc=nfkc)
    return ex.run(root, path if path is not None else element_path(root), as_block=as_block)


# -- non-HTML text ----------------------------------------------------------------------


def text_blocks(text: str, *, kind: str = "line", nfkc: bool = True) -> list[Block]:
    """One block per non-empty line: plain text, pretty-printed JSON, source code."""
    out: list[Block] = []
    for i, line in enumerate(text.splitlines(), 1):
        norm = normalize_text(line, nfkc=nfkc)
        if norm:
            out.append(Block(f"/line[{i}]", kind, norm))
    return out


def json_blocks(text: str, *, nfkc: bool = True) -> list[Block]:
    """Pretty-print with stable key order so a one-field change is a one-line change."""
    try:
        pretty = json.dumps(json.loads(text), indent=1, ensure_ascii=False, sort_keys=True)
    except ValueError:
        return text_blocks(text, nfkc=nfkc)
    return text_blocks(pretty, nfkc=nfkc)
