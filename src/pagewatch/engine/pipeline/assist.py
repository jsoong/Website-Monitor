"""Filter assistant (spec: Desktop UI → Filter assistant): from what the user selected in the
viewer to the filter rules that would hide or isolate it.

The viewer reports the DOM path of the element under the selection (``data-pw-path``, the same
path the extractor gives blocks) and the selected text. This module, which runs in a worker, finds
that element in the stored page, and offers five rules:

* ignore this element: a ``selector`` ignore rule (CSS when a stable one exists, else XPath)
* watch only this element: a ``selector`` watch rule
* ignore text like this: a ``text`` regex with numbers generalised (``\\d+``)
* ignore from page start to here: a ``between`` rule with no start marker
* ignore from here to page end: a ``between`` rule with no end marker

Markers are cut from the selection and checked against the page's blocks, because ``between``
rules find their first occurrence: a marker that also appears earlier would end (or start) the
range in the wrong place, so it is extended to the whole block when that makes it unique. Nothing is
saved here; the caller shows each rule's test result first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pagewatch.engine.pipeline import sources
from pagewatch.engine.pipeline.autofilter import css_selector_for
from pagewatch.engine.pipeline.extract import (
    Block,
    blocks_from_json,
    decode_body,
    element_path,
    normalize_text,
    parse_html,
)
from pagewatch.engine.store.blobs import BlobStore

PATH_RX = re.compile(r"^(?:/[A-Za-z][\w.-]*\[\d+\])+$")
MARKER_CHARS = 80
BLOCK_MARKER_MAX = 300
LONG_SELECTION = 160


@dataclass(slots=True)
class AssistJob:
    blob_root: str
    raw_hash: str
    content_type: str
    blocks_hash: str
    url: str
    source_type: str
    source_cfg: dict[str, Any]
    ignore_case: bool
    path: str
    end_path: str | None = None
    text: str = ""
    levels_up: int = 0


@dataclass(slots=True)
class Draft:
    id: str
    label: str
    section: str  # "ignore" | "watch"
    rule: dict[str, Any]
    explanation: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AssistResult:
    element: dict[str, Any] | None
    selection: str
    block_text: str | None
    drafts: list[Draft]


class AssistError(ValueError):
    """The request cannot be answered (a bad path, or nothing to point at)."""


def check_path(path: str) -> str:
    if not PATH_RX.match(path):
        raise AssistError(f"not a DOM path: {path[:80]!r}")
    return path


def _element(root: Any, path: str) -> Any | None:
    try:
        found = root.xpath(check_path(path))
    except AssistError:
        raise
    except Exception:
        return None
    return found[0] if found else None


def _ancestors(el: Any) -> list[Any]:
    out: list[Any] = []
    node = el
    while node is not None:
        out.append(node)
        node = node.getparent()
    return out


def _common_ancestor(a: Any, b: Any) -> Any:
    mine = {element_path(n): n for n in _ancestors(a)}
    for node in _ancestors(b):
        found = mine.get(element_path(node))
        if found is not None:
            return found
    return a


def _block_for(blocks: list[Block], path: str | None) -> int | None:
    """The block that holds the element at ``path``: the one with the longest path that is the
    element's own path or one of its ancestors'. Failing that, the first block inside it."""
    if not path:
        return None
    best: int | None = None
    for i, b in enumerate(blocks):
        inside = b.path and (path == b.path or path.startswith(b.path + "/"))
        if inside and (best is None or len(b.path) >= len(blocks[best].path)):
            best = i
    if best is not None:
        return best
    for i, b in enumerate(blocks):
        if b.path.startswith(path + "/"):
            return i
    return None


def generalize(text: str) -> str:
    """A regex for ``text`` with every run of digits as ``\\d+`` and spaces as ``\\s+``."""
    out: list[str] = []
    for part in re.split(r"(\d+)", text):
        if not part:
            continue
        if part.isdigit():
            out.append(r"\d+")
        else:
            out.append(re.escape(part).replace(r"\ ", r"\s+"))
    return "".join(out)


def _occurrences(blocks: list[Block], needle: str) -> tuple[int, int | None]:
    """How often ``needle`` occurs in the blocks (any case) and the index of the first one."""
    low = needle.lower()
    total, first = 0, None
    for i, b in enumerate(blocks):
        n = b.text.lower().count(low)
        if n:
            total += n
            if first is None:
                first = i
    return total, first


def pick_marker(
    blocks: list[Block], wanted: str, intended: int | None, side: str
) -> tuple[str, list[str]]:
    """The text that marks the boundary. ``wanted`` is cut from the selection; when it does not
    identify the place the user meant, the whole block (if short enough) is tried."""
    warnings: list[str] = []
    candidates = [wanted]
    if intended is not None:
        whole = blocks[intended].text
        if whole != wanted and len(whole) <= BLOCK_MARKER_MAX and wanted.lower() in whole.lower():
            candidates.append(whole)
    seen: list[tuple[str, int, int | None]] = []
    for cand in candidates:
        total, first = _occurrences(blocks, cand)
        if total == 1 and (intended is None or first == intended):
            return cand, warnings
        seen.append((cand, total, first))
    if all(total == 0 for _, total, _ in seen):
        warnings.append(
            "This text is not in the filtered page, so the rule would do nothing "
            "(it may already be ignored by another filter)."
        )
        return wanted, warnings
    for cand, total, first in seen:
        if intended is not None and first == intended:
            warnings.append(
                f"This text appears {total} times; the rule uses its first occurrence, which "
                "is the one you selected."
            )
            return cand, warnings
    cand, total, _ = seen[0]
    where = "end" if side == "end" else "start"
    warnings.append(
        f"This text appears {total} times and the {where} of the range will be its first "
        "occurrence, which may not be the one you selected. Select a longer or more distinctive "
        "piece of text."
    )
    return cand, warnings


def _segments(text: str) -> list[str]:
    parts = (normalize_text(s) for s in text.split("\n"))
    return [p for p in parts if p]


def assist(job: AssistJob) -> AssistResult:
    store = BlobStore(Path(job.blob_root))
    raw, ctype = sources.view_bytes(
        store.get(job.raw_hash), job.content_type, job.url, job.source_type, job.source_cfg
    )
    root = parse_html(decode_body(raw, ctype))
    blocks = blocks_from_json(store.get_json(job.blocks_hash))
    path, end_path = check_path(job.path), check_path(job.end_path) if job.end_path else None

    start_el = _element(root, path) if root is not None else None
    end_el = _element(root, end_path) if root is not None and end_path else None
    target = None
    levels = 0
    if start_el is not None:
        target = _common_ancestor(start_el, end_el) if end_el is not None else start_el
        for _ in range(job.levels_up):
            parent = target.getparent()
            if parent is None or target.tag in ("body", "html"):
                break
            target = parent
            levels += 1

    start_block = _block_for(blocks, path)
    end_block = _block_for(blocks, end_path) if end_path else start_block
    block_text = blocks[start_block].text if start_block is not None else None

    segments = _segments(job.text)
    from_element = not segments
    if from_element and block_text:
        segments = [block_text]
    selection = " ".join(segments)

    if target is None and not segments:
        raise AssistError("that part of the page could not be found; reload the view and retry")

    drafts: list[Draft] = []
    element_info: dict[str, Any] | None = None
    if target is not None and root is not None:
        css = css_selector_for(target, root)
        selector, kind = (css, "css") if css else (element_path(target), "xpath")
        can_widen = target.getparent() is not None and target.tag not in ("body", "html")
        element_info = {
            "tag": str(target.tag), "path": element_path(target), "selector": selector,
            "selector_kind": kind, "can_widen": can_widen, "levels_up": levels,
        }  # fmt: skip
        whole_page = target.tag in ("body", "html")
        what = f"<{target.tag}> element ({selector})"
        for id_, label, section, why in (
            ("ignore_element", "Ignore this element", "ignore",
             f"Removes the {what} from every check: its text no longer counts."),
            ("watch_element", "Watch only this element", "watch",
             f"Keeps only the {what}: everything outside it no longer counts."),
        ):  # fmt: skip
            warnings = []
            if whole_page:
                warnings.append(
                    "This is the whole page. "
                    + ("Ignoring it would leave nothing to compare." if section == "ignore"
                       else "Watching it changes nothing.")
                )  # fmt: skip
            if (
                start_el is not None
                and end_el is not None
                and levels == 0
                and target is not start_el
            ):
                warnings.append(
                    "The selection spans several elements; their common parent is used."
                )
            drafts.append(
                Draft(
                    id_,
                    label,
                    section,
                    {
                        "type": "selector",
                        "selector": selector,
                        "selector_kind": kind,
                        "note": f"assistant: {label.lower()}",
                    },
                    why,
                    warnings,
                )  # fmt: skip
            )

    if segments:
        first = segments[0][:200]
        warn: list[str] = []
        pattern = generalize(first)
        rx = re.compile(pattern, re.IGNORECASE if job.ignore_case else 0)
        hit_blocks = sum(1 for b in blocks if rx.search(b.text))
        if hit_blocks == 0:
            warn.append(
                "This text is not in the filtered page, so the rule would do nothing "
                "(it may already be ignored by another filter)."
            )
        if len(segments) > 1:
            warn.append("The selection has several lines; the rule uses the first.")
        if from_element:
            warn.append("No text was selected, so the whole block's text is used.")
        if len(first) > LONG_SELECTION:
            warn.append("This is a long piece of text; a shorter selection generalises better.")
        drafts.append(
            Draft(
                "ignore_text", "Ignore text like this (numbers generalized)", "ignore",
                {"type": "text", "pattern": pattern, "pattern_kind": "regex",
                 "note": "assistant: ignore text like this"},
                "Removes text matching this pattern wherever it appears on the page "
                f"({hit_blocks} block{'s' if hit_blocks != 1 else ''} now); numbers match any "
                "number.",
                warn,
            )
        )  # fmt: skip
        head_marker, head_warn = pick_marker(
            blocks, segments[0][:MARKER_CHARS], start_block, "start"
        )
        tail_marker, tail_warn = pick_marker(blocks, segments[-1][-MARKER_CHARS:], end_block, "end")
        drafts.append(
            Draft(
                "ignore_to_here", "Ignore from page start to here", "ignore",
                {"type": "between", "end": tail_marker, "inclusive": True,
                 "note": "assistant: ignore from page start to here"},
                f"Ignores everything from the top of the page down to and including "
                f"“{tail_marker}”.",
                tail_warn,
            )
        )  # fmt: skip
        drafts.append(
            Draft(
                "ignore_from_here", "Ignore from here to page end", "ignore",
                {"type": "between", "start": head_marker, "inclusive": True,
                 "note": "assistant: ignore from here to page end"},
                f"Ignores everything from “{head_marker}” to the end of the page.",
                head_warn,
            )
        )  # fmt: skip
    order = ["ignore_element", "watch_element", "ignore_text", "ignore_to_here", "ignore_from_here"]
    drafts.sort(key=lambda d: order.index(d.id))
    return AssistResult(element_info, selection, block_text, drafts)
