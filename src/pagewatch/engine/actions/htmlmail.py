"""HTML that survives an email client.

Gmail and Outlook.com drop or rewrite ``<style>`` blocks, ignore CSS variables and
``prefers-color-scheme``, and do not run scripts or load remote content by default. So mail
HTML here is: a table layout, every style inline, colours as plain hex, no classes that matter,
no script, no remote images.

The viewer's marked-up fragments (``<ins class="pw-add">`` ...) are converted by
``inline_marks``; ``changes_fragment`` builds the "changes only" body from a diff directly.
"""

from __future__ import annotations

import html as htmllib
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from lxml import html as lxml_html

from pagewatch.engine.pipeline.diff import SEP

ADD = "background:#d9f7d9;color:#0a4d0a;text-decoration:none;"
DEL = "background:#ffe0e0;color:#8a1111;text-decoration:line-through;"
FONT = "font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
MUTED = "color:#626f86;font-size:12px;"

# class on the viewer's HTML -> the inline style that replaces it
CLASS_STYLES: dict[str, str] = {
    "pw-add": ADD + "padding:0 1px;",
    "pw-del": DEL + "padding:0 1px;",
    "pw-ins-block": "background:#d9f7d9;",
    "pw-rep-block": "border-left:3px solid #0a4d0a;padding-left:6px;",
    "pw-changed-cell": "background:#d9f7d9;",
    "pw-moved": "background:#e0ecff;",
    "pw-removed": "background:#fff3f3;",
    "pw-b": "padding:2px 6px;",
}


def esc(text: str) -> str:
    return htmllib.escape(text, quote=True)


def is_web_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("http", "https")


def link(url: str, label: str | None = None) -> str:
    text = esc(label or url)
    if not is_web_url(url):
        return text
    return f'<a href="{esc(url)}" style="color:#0b57d0;text-decoration:underline;">{text}</a>'


# -- conversions ---------------------------------------------------------------------------


def inline_marks(fragment: str) -> str:
    """Turn the viewer's class-based marks into inline styles and drop everything the mail
    client cannot use: the classes themselves, ids, and the side panel of deletions (the
    deletions are already shown in place, struck through)."""
    root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    for panel in root.xpath('.//*[@id="pw-deleted"]'):
        parent = panel.getparent()
        if parent is not None:
            parent.remove(panel)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        classes = (el.get("class") or "").split()
        style = "".join(CLASS_STYLES[c] for c in classes if c in CLASS_STYLES)
        if style:
            el.set("style", (el.get("style") or "") + style)
        for attr in ("class", "id"):
            if attr in el.attrib:
                del el.attrib[attr]
    out = (root.text or "") + "".join(
        lxml_html.tostring(child, encoding="unicode") for child in root
    )
    return out


def marked_line(tokens: Sequence[Sequence[str]]) -> list[str]:
    """One HTML line per block of a replace run, with inline-styled <ins>/<del> marks."""
    lines = [""]
    for kind, text in tokens:
        for k, part in enumerate(text.split(SEP)):
            if k:
                lines.append("")
            if not part:
                continue
            body, space = part.rstrip(), part[len(part.rstrip()) :]
            if kind == "eq":
                lines[-1] += esc(part)
            elif kind == "del":
                lines[-1] += f'<del style="{DEL}padding:0 1px;">{esc(body)}</del>{esc(space)}'
            else:
                lines[-1] += f'<ins style="{ADD}padding:0 1px;">{esc(body)}</ins>{esc(space)}'
    return [ln for ln in lines if ln.strip()]


def _row(inner: str, border: str, bg: str = "") -> str:
    return (
        f'<tr><td style="padding:4px 8px;border-left:3px solid {border};{bg}'
        f'white-space:pre-wrap;word-break:break-word;">{inner}</td></tr>'
    )


def changes_fragment(
    ops: Sequence[dict[str, Any]],
    old_texts: Sequence[str],
    new_texts: Sequence[str],
    *,
    max_blocks: int = 200,
) -> tuple[str, int]:
    """The changed blocks only, as a table of rows (added green, removed red and struck through,
    edited blocks with the changed words marked). Returns ``(html, blocks_not_shown)``."""
    rows: list[str] = []
    total = 0

    def add(row: str) -> None:
        nonlocal total
        total += 1
        if total <= max_blocks:
            rows.append(row)

    for op in ops:
        t = op["t"]
        if t == "ins":
            for x in new_texts[op["new"][0] : op["new"][1] + 1]:
                add(_row(f'<ins style="{ADD}">{esc(x)}</ins>', "#0a4d0a"))
        elif t == "del":
            for x in old_texts[op["old"][0] : op["old"][1] + 1]:
                add(_row(f'<del style="{DEL}">{esc(x)}</del>', "#8a1111"))
        elif t == "rep":
            tokens = op.get("tokens")
            if tokens is None:
                for x in old_texts[op["old"][0] : op["old"][1] + 1]:
                    add(_row(f'<del style="{DEL}">{esc(x)}</del>', "#8a1111"))
                for x in new_texts[op["new"][0] : op["new"][1] + 1]:
                    add(_row(f'<ins style="{ADD}">{esc(x)}</ins>', "#0a4d0a"))
            else:
                for line in marked_line(tokens):
                    add(_row(line, "#b26a00"))
        # eq and mov are not changes
    if not rows:
        return "", 0
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;{FONT}font-size:14px;line-height:1.5;">'
        + "".join(rows)
        + "</table>"
    )
    return table, max(0, total - max_blocks)


# -- the message shell ---------------------------------------------------------------------


def shell(
    *,
    title: str,
    subtitle_html: str,
    meta_html: str,
    body_html: str,
    footer_html: str,
) -> str:
    """A complete HTML document: one centred 640 px column, table layout, inline styles."""
    cell = "padding:14px 20px;"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f"<title>{esc(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f5f7;"><tr><td align="center" style="padding:16px 8px;">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:640px;background:#ffffff;border:1px solid #dfe1e6;'
        f'color:#1d2125;{FONT}font-size:14px;line-height:1.5;">'
        f'<tr><td style="{cell}border-bottom:1px solid #eceef1;">'
        f'<div style="font-size:18px;font-weight:600;">{esc(title)}</div>'
        f'<div style="{MUTED}">{subtitle_html}</div></td></tr>'
        + (f'<tr><td style="{cell}padding-bottom:0;">{meta_html}</td></tr>' if meta_html else "")
        + f'<tr><td style="{cell}">{body_html}</td></tr>'
        f'<tr><td style="{cell}border-top:1px solid #eceef1;{MUTED}">{footer_html}</td></tr>'
        "</table></td></tr></table></body></html>"
    )
