"""Special filters: global toggles applied after the per-region filters, last in the pipeline.

Text-level toggles (NFKC / invisible characters / whitespace, ``<option>`` removal) already
ran during block extraction, which is equivalent because they are idempotent text maps.
"""

from __future__ import annotations

from pagewatch.engine.pipeline.extract import Block
from pagewatch.models import SpecialFilters


def comparison_key(text: str, special: SpecialFilters) -> str:
    return text.lower() if special.ignore_case else text


def apply_special(blocks: list[Block], special: SpecialFilters) -> list[Block]:
    out = list(blocks)
    if special.watch_links:
        seen = dict.fromkeys(url for b in blocks for url in b.links)
        out.extend(Block("", "link", url) for url in seen)
    if special.watch_images:
        seen_img = dict.fromkeys(url for b in blocks for url in b.images)
        out.extend(Block("", "image", url) for url in seen_img)
    if special.sort_content:
        out.sort(key=lambda b: comparison_key(b.text, special))
    return out


def comparison_text(blocks: list[Block], special: SpecialFilters) -> str:
    return "\n".join(comparison_key(b.text, special) for b in blocks)
