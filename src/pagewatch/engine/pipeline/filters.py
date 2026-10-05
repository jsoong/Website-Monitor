"""Cosmetic, watch and ignore filters.

Pipeline order (spec): parse -> cosmetic -> block extraction -> watch -> ignore -> special.

* Cosmetic filters and ``selector`` ignore filters *mark* elements (``data-pw-skip``) instead of
  deleting them, so every block keeps its original DOM path (the in-page diff view needs it).
* ``watch`` rules keep only blocks inside the watched regions (union of all rules).
* ``ignore`` rules remove elements, ranges between markers, text spans, or mask digits.
* ``between`` markers are matched case-insensitively against the normalised block text.

Rules that cannot apply (a selector on a source with no DOM) are skipped, and rules that fail
at run time are skipped with a warning; one bad rule never fails a check.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import lru_cache
from importlib import resources
from typing import Any

from lxml import etree
from lxml.cssselect import CSSSelector

from pagewatch.engine.pipeline.extract import (
    BLOCK_TAGS,
    SKIP_ATTR,
    Block,
    element_path,
    extract_blocks,
    normalize_text,
)
from pagewatch.models import FilterConfig, FilterRule

Warn = Callable[[str], None]

_WS = re.compile(r"\s+")


# -- selectors --------------------------------------------------------------------------


@lru_cache(maxsize=1024)
def _compile(kind: str, selector: str) -> Any:
    return CSSSelector(selector) if kind == "css" else etree.XPath(selector)


def select(root: Any, kind: str, selector: str) -> list[Any]:
    """Elements matched by a CSS or XPath selector, in document order."""
    found = _compile(kind, selector)(root)
    if not isinstance(found, list):
        return []
    return [el for el in found if hasattr(el, "tag") and isinstance(el.tag, str)]


@lru_cache(maxsize=1)
def builtin_cookie_selectors() -> tuple[str, ...]:
    text = (
        resources.files("pagewatch.data")
        .joinpath("cookie_banner_selectors.txt")
        .read_text(encoding="utf-8")
    )
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("# ") or line == "#":
            continue
        try:
            CSSSelector(line)
        except Exception:  # a broken built-in selector must not disable the rest
            continue
        out.append(line)
    return tuple(out)


@lru_cache(maxsize=1)
def _builtin_union() -> str:
    return ", ".join(builtin_cookie_selectors())


def _mark(elements: Sequence[Any]) -> None:
    for el in elements:
        el.set(SKIP_ATTR, "1")


def mark_dom(root: Any, cfg: FilterConfig, warn: Warn) -> None:
    """Cosmetic filters (built-in cookie banners + the bookmark's) and ``selector`` ignore rules."""
    if root is None:
        return
    if cfg.builtin_cosmetic and builtin_cookie_selectors():
        try:
            _mark(select(root, "css", _builtin_union()))
        except Exception as exc:
            warn(f"built-in cosmetic filters failed: {exc}")
    for rule in [*cfg.cosmetic, *(r for r in cfg.ignore if r.type == "selector")]:
        if rule.type != "selector" or not rule.selector:
            continue
        try:
            _mark(select(root, rule.selector_kind, rule.selector))
        except Exception as exc:
            warn(f"selector {rule.selector!r} skipped: {exc}")


# -- scoping ----------------------------------------------------------------------------


def owner_block_path(el: Any) -> str:
    """Path of the nearest block-level ancestor-or-self: the path blocks of this element carry."""
    node = el
    while node is not None:
        tag = node.tag
        if isinstance(tag, str) and (tag in BLOCK_TAGS or tag == "tr"):
            return element_path(node)
        node = node.getparent()
    return element_path(el)


class Scope:
    """Which blocks lie inside the elements a ``scope`` selector matched."""

    def __init__(self, root: Any, selector: str | None, warn: Warn) -> None:
        self.unrestricted = selector is None
        self.prefixes: list[str] = []
        self.exact: set[str] = set()
        if selector is not None and root is not None:
            try:
                for el in select(root, "css", selector):
                    self.prefixes.append(element_path(el))
                    self.exact.add(owner_block_path(el))
            except Exception as exc:
                warn(f"scope {selector!r} skipped: {exc}")

    def contains(self, block: Block) -> bool:
        if self.unrestricted:
            return True
        p = block.path
        return p in self.exact or any(p == q or p.startswith(q + "/") for q in self.prefixes)


# -- text patterns and ranges -----------------------------------------------------------


def text_pattern(rule: FilterRule, ignore_case: bool) -> re.Pattern[str]:
    assert rule.pattern
    flags = re.IGNORECASE if ignore_case else 0
    if rule.pattern_kind == "regex":
        return re.compile(rule.pattern, flags)
    if rule.pattern_kind == "wildcard":
        # `*` is lazy between other text ("Updated * ago") but must run to the end of the
        # block when it is last ("build *"), where a lazy match would match nothing.
        body = re.escape(rule.pattern).replace(r"\?", ".")
        body = body.replace(r"\*", ".*?")
        if rule.pattern.endswith("*"):
            body = body[: -len(".*?")] + ".*"
        return re.compile(body, flags)
    return re.compile(re.escape(rule.pattern), flags)


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _find_ranges(blocks: Sequence[Block], rule: FilterRule) -> list[tuple[int, int, int, int]]:
    """Ranges ``(block_i, offset_i, block_j, offset_j)`` between the rule's markers.

    A missing start marker means start of page and a missing end marker means end of page; a
    marker that is given but not found yields no range (so an ignore filter never silently
    swallows the rest of a restructured page). Markers match case-insensitively; offsets are
    into the original block text."""
    start_rx = re.compile(re.escape(rule.start), re.IGNORECASE) if rule.start else None
    end_rx = re.compile(re.escape(rule.end), re.IGNORECASE) if rule.end else None

    def find(rx: re.Pattern[str], i: int, off: int) -> tuple[int, re.Match[str]] | None:
        while i < len(blocks):
            m = rx.search(blocks[i].text, off)
            if m:
                return i, m
            i, off = i + 1, 0
        return None

    ranges: list[tuple[int, int, int, int]] = []
    bi, off = 0, 0
    while bi < len(blocks):
        if start_rx is None:
            si, lo = 0, 0
            after = (0, 0)
        else:
            hit = find(start_rx, bi, off)
            if hit is None:
                break
            si, m = hit
            after = (si, m.end())
            lo = m.start() if rule.inclusive else m.end()
        if end_rx is None:
            ranges.append((si, lo, len(blocks) - 1, len(blocks[-1].text)))
            break
        found_end = find(end_rx, *after)
        if found_end is None:
            break
        ei, em = found_end
        ranges.append((si, lo, ei, em.end() if rule.inclusive else em.start()))
        if start_rx is None:
            break  # "from start of page to X" is a single range
        bi, off = ei, em.end()
    return ranges


def _slice(blocks: Sequence[Block], rng: tuple[int, int, int, int]) -> list[tuple[int, str]]:
    si, so, ei, eo = rng
    out: list[tuple[int, str]] = []
    for i in range(si, ei + 1):
        text = blocks[i].text
        lo = so if i == si else 0
        hi = eo if i == ei else len(text)
        piece = _clean(text[lo:hi])
        if piece:
            out.append((i, piece))
    return out


def keep_between(blocks: Sequence[Block], rule: FilterRule) -> list[Block]:
    kept: dict[int, str] = {}
    for rng in _find_ranges(blocks, rule):
        for i, piece in _slice(blocks, rng):
            kept[i] = f"{kept[i]} {piece}" if i in kept else piece
    return [replace(blocks[i], text=t) for i, t in sorted(kept.items())]


def remove_between(blocks: Sequence[Block], rule: FilterRule) -> list[Block]:
    cuts: dict[int, list[tuple[int, int]]] = {}
    for si, so, ei, eo in _find_ranges(blocks, rule):
        for i in range(si, ei + 1):
            n = len(blocks[i].text)
            lo = so if i == si else 0
            hi = eo if i == ei else n
            cuts.setdefault(i, []).append((lo, hi))
    out: list[Block] = []
    for i, b in enumerate(blocks):
        spans = cuts.get(i)
        if not spans:
            out.append(b)
            continue
        text, pos, pieces = b.text, 0, []
        for lo, hi in sorted(spans):
            pieces.append(text[pos:lo])
            pos = max(pos, hi)
        pieces.append(text[pos:])
        cleaned = _clean(" ".join(pieces))
        if cleaned:
            out.append(replace(b, text=cleaned))
    return out


# -- watch ------------------------------------------------------------------------------


def _watch_roots(root: Any, rule: FilterRule, warn: Warn) -> list[Any]:
    assert rule.selector
    try:
        found = select(root, rule.selector_kind, rule.selector)
    except Exception as exc:
        warn(f"watch selector {rule.selector!r} skipped: {exc}")
        return []
    kept: list[Any] = []
    kept_paths: list[str] = []
    for el in found:  # document order: a nested match is covered by its container
        p = element_path(el)
        if any(p == q or p.startswith(q + "/") for q in kept_paths):
            continue
        kept.append(el)
        kept_paths.append(p)
    return kept


def collect_blocks(
    root: Any,
    cfg: FilterConfig,
    *,
    base_url: str,
    ignore_options: bool,
    nfkc: bool,
    warn: Warn,
    page_blocks: list[Block] | None = None,
) -> list[Block]:
    """Blocks of the page, or of the watched regions when watch rules exist. For sources with
    no DOM (``root is None``) the already-built ``page_blocks`` stand in for the page."""
    watch = [r for r in cfg.watch if r.type != "selector" or root is not None]
    kwargs: dict[str, Any] = {"base_url": base_url, "ignore_options": ignore_options, "nfkc": nfkc}
    if not watch:
        return extract_blocks(root, **kwargs) if root is not None else list(page_blocks or [])
    index_of: dict[Any, int] | None = None
    if len(watch) > 1 and root is not None:
        index_of = {el: i for i, el in enumerate(root.iter())}
    merged: dict[tuple[str, str], Block] = {}
    page: list[Block] | None = page_blocks if root is None else None
    for rule in watch:
        if rule.type == "selector":
            for region in _watch_roots(root, rule, warn):
                for b in extract_blocks(region, as_block=True, index_of=index_of, **kwargs):
                    merged.setdefault((b.path, b.text), b)
            continue
        if page is None:
            page = extract_blocks(root, index_of=index_of, **kwargs)
        if rule.type == "between":
            for b in keep_between(page, rule):
                merged.setdefault((b.path, b.text), b)
        elif rule.type == "text":
            try:
                pat = text_pattern(rule, cfg.special.ignore_case)
            except re.error as exc:
                warn(f"watch pattern {rule.pattern!r} skipped: {exc}")
                continue
            for b in page:
                spans = [m.group(0) for m in pat.finditer(b.text) if m.group(0).strip()]
                if spans:
                    t = normalize_text(" ".join(spans), nfkc=nfkc)
                    merged.setdefault((b.path, t), replace(b, text=t))
    out = list(merged.values())
    if index_of is not None or len(watch) > 1:
        out.sort(key=lambda b: b.order)
    return out


# -- ignore -----------------------------------------------------------------------------


def apply_ignore(blocks: list[Block], root: Any, cfg: FilterConfig, warn: Warn) -> list[Block]:
    """``between`` / ``text`` / ``number_mask`` ignore rules (selector rules ran as marks)."""
    ignore_case = cfg.special.ignore_case
    for rule in cfg.ignore:
        if rule.type == "selector":
            continue
        if rule.type == "between":
            blocks = remove_between(blocks, rule)
            continue
        scope = Scope(root, rule.scope, warn)
        if rule.scope is not None and not scope.prefixes and not scope.exact:
            continue  # the scoped element is not on this page (right now): nothing to do
        if rule.type == "text":
            try:
                pat = text_pattern(rule, ignore_case)
            except re.error as exc:
                warn(f"ignore pattern {rule.pattern!r} skipped: {exc}")
                continue
            out: list[Block] = []
            for b in blocks:
                if not scope.contains(b):
                    out.append(b)
                    continue
                cleaned = _clean(pat.sub("", b.text))
                if cleaned:
                    out.append(replace(b, text=cleaned))
            blocks = out
        elif rule.type == "number_mask":
            pat = None
            if rule.pattern:
                try:
                    pat = text_pattern(rule, ignore_case)
                except re.error as exc:
                    warn(f"number_mask pattern {rule.pattern!r} skipped: {exc}")
                    continue
            blocks = [
                replace(b, text=re.sub(r"\d", "#", b.text))
                if scope.contains(b) and (pat is None or pat.search(b.text))
                else b
                for b in blocks
            ]
    return blocks
