"""HTML views of a diff for the UI (and "changes only" emails).

* ``render_text`` is the always-working text view: normalised blocks with inline marks.
* ``render_highlight`` re-injects ``<ins class="pw-add">`` and ``<del class="pw-del">`` into the
  *new version's own HTML* at each changed block's DOM path. Changed character offsets are mapped
  back through the raw text nodes, so highlights survive links, bold text and other inline markup;
  where that mapping is not possible (rows, text altered by a text filter) the whole block or cell
  is marked instead.
* ``render_plain`` shows a stored version unmarked.

Everything is sanitised with nh3 (scripts, event handlers and forms removed) and wrapped with a
Content-Security-Policy that blocks remote resources unless the caller turns them on.
"""

from __future__ import annotations

import html as htmllib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import nh3
from lxml import etree

from pagewatch.engine.pipeline.diff import SEP, DiffResult
from pagewatch.engine.pipeline.extract import (
    _INVISIBLE,
    Block,
    decode_body,
    extract_blocks,
    parse_html,
)

CSS = """
:root { color-scheme: light dark; }
body.pw-view { font: 15px/1.5 system-ui, "Segoe UI", sans-serif; margin: 1rem 1.25rem; max-width: 70rem;
  --add-bg: #d9f7d9; --add-fg: #0a4d0a; --del-bg: #ffe0e0; --del-fg: #8a1111; --mov-bg: #e0ecff; }
@media (prefers-color-scheme: dark) {
  body.pw-view { --add-bg: #1e4620; --add-fg: #c9f5c9; --del-bg: #5a2020; --del-fg: #ffd0d0; --mov-bg: #1d3557; }
}
ins.pw-add { background: var(--add-bg); color: var(--add-fg); text-decoration: none; padding: 0 1px; border-radius: 2px; }
del.pw-del { background: var(--del-bg); color: var(--del-fg); text-decoration: line-through; padding: 0 1px; border-radius: 2px; }
.pw-ins-block { outline: 2px solid var(--add-bg); background: var(--add-bg); }
.pw-rep-block { box-shadow: inset 3px 0 0 var(--add-fg); }
.pw-changed-cell { background: var(--add-bg); }
.pw-moved { background: var(--mov-bg); }
.pw-removed { opacity: .85; }
.pw-b { padding: 1px 6px; border-left: 3px solid transparent; white-space: pre-wrap; }
.pw-b.pw-ins, .pw-b.pw-rep { border-left-color: var(--add-fg); }
.pw-b.pw-del { border-left-color: var(--del-fg); }
.pw-b.pw-mov { border-left-color: #3b78d8; background: var(--mov-bg); }
.pw-gap { color: gray; padding: 0 6px; }
#pw-deleted { display: none; border-top: 1px solid gray; margin-top: 2rem; }
body.pw-del-panel #pw-deleted { display: block; }
body.pw-del-panel .pw-removed, body.pw-del-panel del.pw-del { display: none; }
body.pw-del-none .pw-removed, body.pw-del-none del.pw-del { display: none; }
.pw-note { padding: .5rem 1rem; background: #fff4ce; color: #5c4400; border-radius: 4px; margin-bottom: 1rem; }
"""

_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote", "br", "caption",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr",
    "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol", "p", "pre", "q", "s", "samp",
    "section", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "time", "tr", "u", "ul", "var", "wbr", "body", "html", "head",
    "title", "meta", "style", "link", "base",
}  # fmt: skip
_ATTRS = {
    "*": {"class", "id", "title", "lang", "dir"},
    "a": {"href", "rel"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "meta": {"charset", "http-equiv", "content", "name"},
    "link": {"rel", "href"},
    "base": {"href"},
}


# -- sanitising and wrapping --------------------------------------------------------------


def _csp(allow_remote: bool) -> str:
    if allow_remote:
        return (
            "default-src 'none'; img-src data: http: https:; style-src 'unsafe-inline' http: https:"
        )
    return "default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def sanitize_fragment(markup: str, *, allow_remote: bool = False) -> str:
    """Strip scripts, event handlers, forms and (unless allowed) remote resources."""
    tags = _TAGS - {"head", "html", "body", "title", "meta", "style", "base"}
    if not allow_remote:
        tags -= {"link"}
    cleaned: str = nh3.clean(
        markup,
        tags=tags,
        clean_content_tags={"script", "style", "template", "noscript", "iframe", "object", "embed"},
        attributes={k: v for k, v in _ATTRS.items() if k in tags or k == "*"},
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
        set_tag_attribute_values={"a": {"rel": "noopener noreferrer nofollow"}},
        strip_comments=True,
    )
    return cleaned


def wrap_document(body: str, *, allow_remote: bool = False, base_url: str | None = None,
                  body_class: str = "pw-view pw-del-inline") -> str:  # fmt: skip
    base = (
        f'<base href="{htmllib.escape(base_url, quote=True)}">' if allow_remote and base_url else ""
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{_csp(allow_remote)}">'
        f'{base}<style>{CSS}</style></head><body class="{body_class}">{body}</body></html>'
    )


def _strip_remote(root: Any, allow_remote: bool) -> None:
    """Without remote resources images become a text placeholder (their source is never loaded)."""
    for img in list(root.iter("img")):
        if allow_remote:
            continue
        alt = (img.get("alt") or "image").strip()
        span = etree.Element("span")
        span.set("class", "pw-img")
        span.text = f"[{alt}]"
        span.tail = img.tail
        parent = img.getparent()
        if parent is not None:
            parent.replace(img, span)
    for link in list(root.iter("link")):
        if not allow_remote or (link.get("rel") or "").lower() != "stylesheet":
            parent = link.getparent()
            if parent is not None:
                parent.remove(link)


# -- text view ----------------------------------------------------------------------------


def _esc(text: str) -> str:
    return htmllib.escape(text, quote=False)


def _marked_html(tokens: list[list[str]]) -> list[str]:
    lines = [""]
    for kind, text in tokens:
        for k, part in enumerate(text.split(SEP)):
            if k:
                lines.append("")
            if not part:
                continue
            if kind == "eq":
                lines[-1] += _esc(part)
            elif kind == "del":
                lines[-1] += (
                    f'<del class="pw-del">{_esc(part.rstrip())}</del>{_esc(part[len(part.rstrip()) :])}'
                )
            else:
                lines[-1] += (
                    f'<ins class="pw-add">{_esc(part.rstrip())}</ins>{_esc(part[len(part.rstrip()) :])}'
                )
    return [ln for ln in lines if ln.strip()]


def render_text(
    diff: DiffResult,
    old_texts: Sequence[str],
    new_texts: Sequence[str],
    *,
    context: int | None = None,
) -> str:
    """The text view as an HTML fragment. ``context`` collapses long unchanged runs."""
    out: list[str] = []
    for op in diff.ops:
        t = op["t"]
        if t == "eq":
            block = new_texts[op["new"][0] : op["new"][1] + 1]
            if context is not None and len(block) > 2 * context:
                out += [f'<div class="pw-b">{_esc(x)}</div>' for x in block[:context]]
                out.append(
                    f'<div class="pw-gap">… {len(block) - 2 * context} unchanged blocks …</div>'
                )
                out += [f'<div class="pw-b">{_esc(x)}</div>' for x in block[len(block) - context :]]
            else:
                out += [f'<div class="pw-b">{_esc(x)}</div>' for x in block]
        elif t == "ins":
            out += [
                f'<div class="pw-b pw-ins"><ins class="pw-add">{_esc(x)}</ins></div>'
                for x in new_texts[op["new"][0] : op["new"][1] + 1]
            ]
        elif t == "del":
            out += [
                f'<div class="pw-b pw-del pw-removed"><del class="pw-del">{_esc(x)}</del></div>'
                for x in old_texts[op["old"][0] : op["old"][1] + 1]
            ]
        elif t == "mov":
            out += [
                f'<div class="pw-b pw-mov" title="moved">{_esc(x)}</div>'
                for x in new_texts[op["new"][0] : op["new"][1] + 1]
            ]
        else:
            tokens = op.get("tokens")
            if tokens is None:
                out += [
                    f'<div class="pw-b pw-del pw-removed"><del class="pw-del">{_esc(x)}</del></div>'
                    for x in old_texts[op["old"][0] : op["old"][1] + 1]
                ]
                out += [
                    f'<div class="pw-b pw-ins"><ins class="pw-add">{_esc(x)}</ins></div>'
                    for x in new_texts[op["new"][0] : op["new"][1] + 1]
                ]
            else:
                out += [f'<div class="pw-b pw-rep">{x}</div>' for x in _marked_html(tokens)]
    if not out:
        out = ['<div class="pw-note">This page has no text content.</div>']
    note = (
        '<div class="pw-note">This change was too large for a word-by-word comparison; '
        "changed blocks are shown whole.</div>"
        if diff.degraded
        else ""
    )
    return note + "\n".join(out)


# -- in-page highlight --------------------------------------------------------------------


@dataclass(slots=True)
class BlockMarks:
    """New-side text of one block of a replace run with its inserted spans and the old text
    that was deleted at given offsets."""

    text: str
    ins: list[tuple[int, int]] = field(default_factory=list)
    dels: list[tuple[int, str]] = field(default_factory=list)


def block_marks(tokens: list[list[str]]) -> list[BlockMarks]:
    out: list[BlockMarks] = []
    cur = BlockMarks("")
    for kind, text in tokens:
        for k, part in enumerate(text.split(SEP)):
            if k:
                out.append(cur)
                cur = BlockMarks("")
            if not part:
                continue
            if kind == "del":
                if part.strip():
                    cur.dels.append((len(cur.text), part.strip()))
                continue
            if kind == "ins" and part.strip():
                cur.ins.append((len(cur.text), len(cur.text) + len(part.rstrip())))
            cur.text += part
    out.append(cur)
    return out


def _map_offsets(
    pieces: Sequence[tuple[Any, str, str]], nfkc: bool
) -> tuple[str, list[tuple[int, int]]]:
    """Normalised text of the pieces and, for each of its characters, ``(piece, offset)``."""
    out: list[str] = []
    where: list[tuple[int, int]] = []
    for pi, (_, _, raw) in enumerate(pieces):
        for oi, ch in enumerate(raw):
            s = unicodedata.normalize("NFKC", ch) if nfkc else ch
            if nfkc:
                s = _INVISIBLE.sub("", s)
            for c in s:
                if c.isspace():
                    if out and out[-1] != " ":
                        out.append(" ")
                        where.append((pi, oi))
                else:
                    out.append(c)
                    where.append((pi, oi))
    if out and out[-1] == " ":
        out.pop()
        where.pop()
    return "".join(out), where


def _apply_segments(owner: Any, attr: str, segments: list[tuple[str, str]]) -> None:
    first = ""
    nodes: list[Any] = []
    for kind, txt in segments:
        if kind == "text":
            if nodes:
                nodes[-1].tail = (nodes[-1].tail or "") + txt
            else:
                first += txt
        else:
            el = etree.Element("ins" if kind == "ins" else "del")
            el.set("class", "pw-add" if kind == "ins" else "pw-del")
            el.text = txt
            nodes.append(el)
    if attr == "text":
        owner.text = first or None
        for i, el in enumerate(nodes):
            owner.insert(i, el)
    else:
        parent = owner.getparent()
        if parent is None:
            return
        owner.tail = first or None
        base = parent.index(owner) + 1
        for i, el in enumerate(nodes):
            parent.insert(base + i, el)


def _inject(block: Block, marks: BlockMarks, nfkc: bool) -> bool:
    """Wrap the inserted spans of ``marks`` and add deletion markers inside the raw text
    nodes of ``block``. Returns False (nothing touched) if the offsets cannot be mapped."""
    text, where = _map_offsets(block.pieces, nfkc)
    if text != marks.text or not block.pieces:
        return False
    edits: dict[int, list[tuple[int, str, int, str]]] = {}  # piece -> (start, kind, end, text)
    for a, b in marks.ins:
        if a >= b or b > len(where):
            continue
        run_piece, run_start = where[a]
        run_end = where[a][1]
        for k in range(a + 1, b):
            pi, oi = where[k]
            if pi != run_piece:
                edits.setdefault(run_piece, []).append((run_start, "ins", run_end + 1, ""))
                run_piece, run_start = pi, oi
            run_end = oi
        edits.setdefault(run_piece, []).append((run_start, "ins", run_end + 1, ""))
    for off, old in marks.dels:
        if off < len(where):
            pi, oi = where[off]
        elif where:
            pi, oi = where[-1][0], where[-1][1] + 1
        else:
            continue
        edits.setdefault(pi, []).append((oi, "del", oi, old))
    for pi, items in edits.items():
        owner, attr, raw = block.pieces[pi]
        segments: list[tuple[str, str]] = []
        cursor = 0
        for start, kind, end, old in sorted(items, key=lambda e: (e[0], e[1] != "del")):
            if start > cursor:
                segments.append(("text", raw[cursor:start]))
                cursor = start
            if kind == "del":
                segments.append(("del", old))
            else:
                piece = raw[start:end]
                core = piece.strip()
                if core:  # keep edge whitespace outside the mark
                    lead = len(piece) - len(piece.lstrip())
                    if lead:
                        segments.append(("text", piece[:lead]))
                    segments.append(("ins", core))
                    if len(piece) - lead - len(core):
                        segments.append(("text", piece[lead + len(core) :]))
                else:
                    segments.append(("text", piece))
                cursor = end
        if cursor < len(raw):
            segments.append(("text", raw[cursor:]))
        _apply_segments(owner, attr, segments)
    return True


def _add_class(el: Any, cls: str) -> None:
    el.set("class", f"{el.get('class', '')} {cls}".strip())


_ROW_CELLS = ("td", "th")


def _mark_row_cells(row: Any, old: str | None, new: str) -> None:
    """Mark the changed cells of a table row (coarse: the whole row if they do not line up)."""
    cells = [c for c in row if isinstance(c.tag, str) and c.tag in _ROW_CELLS]
    new_cells = new.split(" | ")
    old_cells = old.split(" | ") if old is not None else None
    if old_cells is None or len(old_cells) != len(new_cells) or len(cells) != len(new_cells):
        _add_class(row, "pw-rep-block")
        return
    _add_class(row, "pw-rep-block")
    for c, a, b in zip(cells, old_cells, new_cells, strict=True):
        if a != b:
            _add_class(c, "pw-changed-cell")


@dataclass(slots=True)
class _Info:
    kind: str  # ins | rep | mov
    marks: BlockMarks | None = None
    old_text: str | None = None


def _collect_info(
    diff: DiffResult, old_texts: Sequence[str], new_texts: Sequence[str]
) -> tuple[dict[int, _Info], list[tuple[int | None, str]]]:
    info: dict[int, _Info] = {}
    removed: list[tuple[int | None, str]] = []
    ops = diff.ops
    for n, op in enumerate(ops):
        t = op["t"]
        if t == "ins":
            for j in range(op["new"][0], op["new"][1] + 1):
                info[j] = _Info("ins")
        elif t == "mov":
            for j in range(op["new"][0], op["new"][1] + 1):
                info[j] = _Info("mov")
        elif t == "rep":
            news = list(range(op["new"][0], op["new"][1] + 1))
            olds = list(range(op["old"][0], op["old"][1] + 1))
            tokens = op.get("tokens")
            per = block_marks(tokens) if tokens is not None else None
            for idx, j in enumerate(news):
                pair_old = old_texts[olds[idx]] if len(olds) == len(news) else None
                marks = per[idx] if per is not None and len(per) == len(news) else None
                info[j] = _Info("rep", marks, pair_old)
        elif t == "del":
            nxt = next((o["new"][0] for o in ops[n + 1 :] if "new" in o), None)
            for i in range(op["old"][0], op["old"][1] + 1):
                removed.append((nxt, old_texts[i]))
    return info, removed


def render_highlight(
    raw: bytes,
    content_type: str,
    url: str,
    new_blocks: Sequence[Block],
    old_texts: Sequence[str],
    diff: DiffResult,
    *,
    nfkc: bool = True,
    ignore_options: bool = True,
    allow_remote: bool = False,
) -> str | None:
    """The new version's HTML with changes marked in place; ``None`` if ``raw`` is not HTML
    (the caller falls back to the text view)."""
    text = decode_body(raw, content_type)
    if not _looks_like_html(content_type, text):
        return None
    root = parse_html(text)
    if root is None:
        return None
    new_texts = [b.text for b in new_blocks]
    info, removed = _collect_info(diff, old_texts, new_texts)
    recorded = extract_blocks(
        root, base_url=url, ignore_options=ignore_options, nfkc=nfkc, record=True
    )
    by_path: dict[str, list[Block]] = {}
    for rb in recorded:
        by_path.setdefault(rb.path, []).append(rb)
    elements: dict[str, Any] = {}

    def element(path: str) -> Any | None:
        if path not in elements:
            try:
                found = root.xpath(path)
            except etree.XPathError:
                found = []
            elements[path] = found[0] if found else None
        return elements[path]

    targets: dict[int, Any] = {}
    for j, nb in enumerate(new_blocks):
        inf = info.get(j)
        el = element(nb.path) if nb.path else None
        if el is not None:
            targets[j] = el
        if inf is None or el is None:
            continue
        if inf.kind == "mov":
            _add_class(el, "pw-moved")
            continue
        if nb.kind == "tr":
            if inf.kind == "ins":
                _add_class(el, "pw-ins-block")
            else:
                _mark_row_cells(el, inf.old_text, nb.text)
            continue
        source = next((b for b in by_path.get(nb.path, []) if b.text == nb.text), None)
        if inf.kind == "ins":
            marks = BlockMarks(nb.text, [(0, len(nb.text))])
            if source is None or not _inject(source, marks, nfkc):
                _add_class(el, "pw-ins-block")
        elif inf.marks is not None and source is not None and _inject(source, inf.marks, nfkc):
            _add_class(el, "pw-rep-block")
        else:
            _add_class(el, "pw-ins-block" if inf.marks is None else "pw-rep-block")

    body = root.find(".//body")
    if body is None:
        body = root
    for before_j, text in removed:  # blocks that no longer exist: shown struck through in place
        anchor = targets.get(before_j) if before_j is not None else None
        tag = anchor.tag if anchor is not None and isinstance(anchor.tag, str) else "div"
        holder: Any
        if tag == "tr":
            holder = etree.Element("tr")
            cell = etree.SubElement(holder, "td")
            cell.set("colspan", "99")
            host: Any = cell
        elif tag == "li":
            holder = host = etree.Element("li")
        else:
            holder = host = etree.Element("div")
        holder.set("class", "pw-removed")
        d = etree.SubElement(host, "del")
        d.set("class", "pw-del")
        d.text = text
        if anchor is not None:
            anchor.addprevious(holder)
        else:
            body.append(holder)
    if removed:
        panel = etree.SubElement(body, "aside")
        panel.set("id", "pw-deleted")
        head = etree.SubElement(panel, "h2")
        head.text = "Removed"
        ul = etree.SubElement(panel, "ul")
        for _, text in removed:
            li = etree.SubElement(ul, "li")
            d = etree.SubElement(li, "del")
            d.set("class", "pw-del")
            d.text = text
    _strip_remote(root, allow_remote)
    markup = etree.tostring(body, encoding="unicode", method="html")
    note = (
        '<div class="pw-note">This change was too large for a word-by-word comparison.</div>'
        if diff.degraded
        else ""
    )
    return note + sanitize_fragment(markup, allow_remote=allow_remote)


def render_plain(raw: bytes, content_type: str, url: str, *, allow_remote: bool = False,
                 nfkc: bool = True) -> str:  # fmt: skip
    """A stored version, unmarked (the New / Old tabs)."""
    text = decode_body(raw, content_type)
    root = parse_html(text)
    if root is None or not _looks_like_html(content_type, text):
        return f"<pre>{_esc(text)}</pre>"
    _strip_remote(root, allow_remote)
    body = root.find(".//body")
    markup = etree.tostring(body if body is not None else root, encoding="unicode", method="html")
    return sanitize_fragment(markup, allow_remote=allow_remote)


_HTMLISH = re.compile(r"<\s*(html|body|div|p|table|ul|ol|h[1-6])\b", re.IGNORECASE)


def _looks_like_html(content_type: str, text: str) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    return "html" in ct or (not ct and bool(_HTMLISH.search(text[:4096])))
