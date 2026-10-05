"""Automatic filters: from a change the user flags as a false positive, propose ignore rules.

For every changed block:

* if the *only* difference between the old and new text is a span matching a known volatile
  pattern (``data/volatile_patterns.yaml``: dates, times, "N minutes ago", counters, hex
  tokens, currency amounts), propose a **text** ignore rule with that pattern, scoped to the
  block's element when a stable CSS selector for it exists;
* otherwise propose a **selector** ignore on the block's smallest containing element.

Every proposal is verified by re-running the comparison with the rule added: a proposal is
only ``verified`` if the false positive disappears. Nothing is saved here; the user confirms.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from lxml.cssselect import CSSSelector

from pagewatch.engine.pipeline import sources
from pagewatch.engine.pipeline.core import build_blocks
from pagewatch.engine.pipeline.diff import DiffResult, diff_blocks
from pagewatch.engine.pipeline.extract import Block, decode_body, element_path, parse_html
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.models import FilterConfig, FilterRule, HighlightMode

_WS = re.compile(r"\s+")
_IDENT = re.compile(r"^[A-Za-z_][\w-]*$")
# Generated names (CSS-in-JS, build hashes) are not stable enough to hang a filter on.
_GENERATED = re.compile(
    r"^(?:css|sc|jss|jsx|styled|emotion|svelte|_)[-_]?[a-z0-9]{4,}$|[0-9a-f]{8,}|\d{3,}",
    re.IGNORECASE,
)


def _generated(name: str) -> bool:
    letters = sum(c.isalpha() for c in name)
    digits = sum(c.isdigit() for c in name)
    return bool(_GENERATED.search(name)) or (digits >= 2 and letters >= 1 and len(name) >= 6)


@dataclass(slots=True)
class VolatilePattern:
    name: str
    description: str
    regex: re.Pattern[str]


@lru_cache(maxsize=1)
def volatile_patterns() -> tuple[VolatilePattern, ...]:
    text = (
        resources.files("pagewatch.data")
        .joinpath("volatile_patterns.yaml")
        .read_text(encoding="utf-8")
    )
    out: list[VolatilePattern] = []
    for item in yaml.safe_load(text)["patterns"]:
        out.append(
            VolatilePattern(
                item["name"], item.get("description", ""), re.compile(item["regex"], re.IGNORECASE)
            )
        )
    return tuple(out)


@dataclass(slots=True)
class Proposal:
    rule: dict[str, Any]
    kind: str  # "volatile_pattern" | "element"
    pattern_name: str | None
    explanation: str
    example_old: str
    example_new: str
    verified: bool = False  # alone, it makes the false positive disappear


@dataclass(slots=True)
class ProposalResult:
    proposals: list[Proposal] = field(default_factory=list)
    resolves_all: bool = False  # all verified proposals together remove the false positive
    remaining_changed_blocks: int = 0


# -- selectors --------------------------------------------------------------------------


def _unique(root: Any, selector: str, el: Any) -> bool:
    try:
        found = CSSSelector(selector)(root)
    except Exception:
        return False
    return len(found) == 1 and found[0] is el


def _classes(el: Any) -> list[str]:
    return [c for c in (el.get("class") or "").split() if _IDENT.match(c) and not _generated(c)]


def css_selector_for(el: Any, root: Any) -> str | None:
    """A short CSS selector that matches exactly ``el``, or ``None``. Prefers an id, then
    ``tag.class`` (unique), then a short chain of ancestors; ignores generated class names."""
    node, parts = el, []
    for _ in range(5):
        if node is None or not isinstance(node.tag, str):
            break
        ident = node.get("id")
        if ident and _IDENT.match(ident) and not _generated(ident):
            parts.append(f"#{ident}")
            sel = " > ".join(reversed(parts))
            if _unique(root, sel, el):
                return sel
            break
        step = node.tag + "".join(f".{c}" for c in _classes(node)[:2])
        parts.append(step)
        sel = " > ".join(reversed(parts))
        if _unique(root, sel, el):
            return sel
        node = node.getparent()
    return None


def _element_by_path(root: Any, path: str) -> Any | None:
    if not path:
        return None
    try:
        found = root.xpath(re.sub(r"\[(\d+)\]", r"[\1]", path))
    except Exception:
        return None
    return found[0] if found else None


# -- proposals --------------------------------------------------------------------------


def _squash(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _volatile_for(old: str | None, new: str | None) -> VolatilePattern | None:
    """The first pattern whose removal makes the two texts identical."""
    if old is None or new is None:
        return None
    for p in volatile_patterns():
        if (
            p.regex.search(new)
            and p.regex.search(old)
            and _squash(p.regex.sub("", old)) == _squash(p.regex.sub("", new))
        ):
            return p
    return None


def _candidates(
    diff: DiffResult, old: list[Block], new: list[Block]
) -> list[tuple[Block | None, Block | None]]:
    """``(old_block, new_block)`` pairs for every changed block."""
    out: list[tuple[Block | None, Block | None]] = []
    for op in diff.ops:
        t = op["t"]
        if t == "ins":
            out += [(None, new[j]) for j in range(op["new"][0], op["new"][1] + 1)]
        elif t == "del":
            out += [(old[i], None) for i in range(op["old"][0], op["old"][1] + 1)]
        elif t == "rep":
            olds = list(range(op["old"][0], op["old"][1] + 1))
            news = list(range(op["new"][0], op["new"][1] + 1))
            if len(olds) == len(news):
                out += [(old[i], new[j]) for i, j in zip(olds, news, strict=True)]
            else:
                joined = Block("", "div", " ".join(old[i].text for i in olds))
                out += [(joined, new[j]) for j in news] or [(old[i], None) for i in olds]
    return out


class MemoryBlobStore:
    """In-memory content-addressed store: lets ``propose`` run over samples that must never
    be persisted (the add-bookmark preview)."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self._data[digest] = data
        return digest

    def get(self, digest: str) -> bytes:
        return self._data[digest]


def propose(
    store: BlobStore | MemoryBlobStore,
    *,
    old_raw: str,
    new_raw: str,
    old_ctype: str,
    new_ctype: str,
    url: str,
    source_type: str,
    filter_cfg: dict[str, Any],
    highlight_mode: str,
    source_cfg: dict[str, Any] | None = None,
) -> ProposalResult:
    fcfg = FilterConfig.model_validate(filter_cfg)
    mode = HighlightMode(highlight_mode)

    def build(raw: str, ctype: str, cfg: FilterConfig) -> list[Block]:
        return build_blocks(store.get(raw), ctype, url, source_type, cfg, raw, None, source_cfg)

    def changed(cfg: FilterConfig) -> int:
        d = diff_blocks(
            [b.text for b in build(old_raw, old_ctype, cfg)],
            [b.text for b in build(new_raw, new_ctype, cfg)],
            ignore_case=cfg.special.ignore_case,
            detect_moves=mode is not HighlightMode.EXACT,
        )
        return 0 if d.is_empty else d.changed_blocks or 1

    old_blocks, new_blocks = build(old_raw, old_ctype, fcfg), build(new_raw, new_ctype, fcfg)
    diff = diff_blocks(
        [b.text for b in old_blocks],
        [b.text for b in new_blocks],
        ignore_case=fcfg.special.ignore_case,
        detect_moves=mode is not HighlightMode.EXACT,
    )

    def dom(raw: str, ctype: str) -> Any:
        body, kind_type = sources.view_bytes(store.get(raw), ctype, url, source_type, source_cfg)
        return parse_html(decode_body(body, kind_type))

    new_root = dom(new_raw, new_ctype)
    old_root = dom(old_raw, old_ctype)

    seen: set[str] = set()
    proposals: list[Proposal] = []
    for ob, nb in _candidates(diff, old_blocks, new_blocks):
        block = nb or ob
        assert block is not None
        root = new_root if nb is not None else old_root
        element = _element_by_path(root, block.path) if root is not None else None
        selector = css_selector_for(element, root) if element is not None else None
        pattern = _volatile_for(ob.text if ob else None, nb.text if nb else None)
        rule: FilterRule | None = None
        if pattern is not None:
            rule = FilterRule(
                type="text", pattern=pattern.regex.pattern, pattern_kind="regex", scope=selector,
                note=f"auto: {pattern.name}",
            )  # fmt: skip
            kind, name = "volatile_pattern", pattern.name
            why = (
                f"the only difference is a {pattern.name.replace('_', ' ')} ({pattern.description})"
            )
        elif element is not None:
            if selector is not None:
                rule = FilterRule(type="selector", selector=selector, note="auto: element")
            else:
                rule = FilterRule(
                    type="selector", selector=element_path(element), selector_kind="xpath",
                    note="auto: element",
                )  # fmt: skip
            kind, name = "element", None
            why = "this block changed and has no recognisable volatile pattern"
        if rule is None:
            continue
        dumped = rule.model_dump(mode="json", exclude_defaults=True)
        key = repr(sorted(dumped.items()))
        if key in seen:
            continue
        seen.add(key)
        proposals.append(
            Proposal(
                dumped,
                kind,
                name,
                why,
                (ob.text if ob else "")[:200],
                (nb.text if nb else "")[:200],
            )
        )

    base = fcfg.model_dump(mode="json")

    def with_rules(rules: list[dict[str, Any]]) -> FilterConfig:
        return FilterConfig.model_validate({**base, "ignore": [*base["ignore"], *rules]})

    for p in proposals:
        p.verified = changed(with_rules([p.rule])) == 0
    remaining = changed(with_rules([p.rule for p in proposals])) if proposals else changed(fcfg)
    return ProposalResult(proposals, remaining == 0 and bool(proposals), remaining)


def load_store(blob_root: str) -> BlobStore:
    return BlobStore(Path(blob_root))


@dataclass(slots=True)
class ProposeJob:
    """Picklable arguments for running ``propose`` in a worker process."""

    blob_root: str
    old_raw: str
    new_raw: str
    old_ctype: str
    new_ctype: str
    url: str
    source_type: str
    filter_cfg: dict[str, Any]
    highlight_mode: str
    source_cfg: dict[str, Any] = field(default_factory=dict)


def propose_job(job: ProposeJob) -> ProposalResult:
    return propose(
        load_store(job.blob_root),
        old_raw=job.old_raw,
        new_raw=job.new_raw,
        old_ctype=job.old_ctype,
        new_ctype=job.new_ctype,
        url=job.url,
        source_type=job.source_type,
        filter_cfg=job.filter_cfg,
        highlight_mode=job.highlight_mode,
        source_cfg=job.source_cfg,
    )
