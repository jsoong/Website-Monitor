"""Alert gating: turn a detected difference into an alert only if every rule passes.

Rule order (first failure wins, and the reason is recorded):

 1. HTTP status is 2xx/304            (runner; an error path, not a change)
 2. readable characters >= min_chars   -> too_short        bad fetch: version not stored
 3. no blacklist phrase on the page    -> blacklist        bad fetch: version not stored
 4. a whitelist phrase present, if any -> whitelist_miss   bad fetch: version not stored
 5. something added/modified, if "ignore removed content" -> removed_only
 6. a keyword rule matches             -> keyword_miss
 7. changed words >= threshold         -> below_threshold  (skipped after a keyword hit)
 8. plugin hooks

Rules 2-4 reject bad fetches, which never advance any pointer, so an error page can never
become the comparison base. Every other outcome stores the version.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pagewatch.engine.pipeline.diff import DiffResult
from pagewatch.models import GateConfig, ThresholdMode


@dataclass(slots=True)
class GateVerdict:
    alert: bool
    store: bool = True  # False: a bad fetch; no pointer advances
    reason: str | None = None
    keyword_hits: list[str] = field(default_factory=list)


def check_bad_fetch(cfg: GateConfig, texts: Sequence[str]) -> GateVerdict | None:
    """Rules 2-4 on the new page. Returns a rejecting verdict, or ``None`` if the page is fine."""
    if cfg.min_chars and sum(len(t) for t in texts) < cfg.min_chars:
        return GateVerdict(alert=False, store=False, reason="too_short")
    if cfg.blacklist or cfg.whitelist:
        page = "\n".join(texts).lower()
        for phrase in cfg.blacklist:
            if phrase.strip() and phrase.lower() in page:
                return GateVerdict(alert=False, store=False, reason="blacklist")
        phrases = [p for p in cfg.whitelist if p.strip()]
        if phrases and not any(p.lower() in page for p in phrases):
            return GateVerdict(alert=False, store=False, reason="whitelist_miss")
    return None


def uses_anchor(cfg: GateConfig) -> bool:
    """Cumulative thresholds compare the new page with the gate anchor, not with the
    previous check."""
    return cfg.threshold_mode is ThresholdMode.CUMULATIVE and cfg.min_changed_words > 0


def threshold_words(diff: DiffResult, cfg: GateConfig) -> int:
    return diff.added_words if cfg.ignore_removed else diff.changed_words


def check_change(
    cfg: GateConfig,
    *,
    latest_diff: DiffResult,
    anchor_diff: DiffResult | None,
    keywords_configured: bool = False,
    keyword_hits: list[str] | None = None,
) -> GateVerdict:
    """Rules 5-7 for a version that passed the bad-fetch rules.

    ``keyword_hits`` are the rules that fired on the changes since the latest version
    (computed by the caller, which owns the page text); ``keywords_configured`` says whether
    there were any keyword rules at all."""
    hits = keyword_hits or []
    if latest_diff.is_empty:
        # only moved blocks: a reorder is not a change in Standard / Table mode
        return GateVerdict(alert=False, reason="reorder_only")
    if cfg.ignore_removed and not latest_diff.has_additions:
        return GateVerdict(alert=False, reason="removed_only")
    if keywords_configured and not hits:
        return GateVerdict(alert=False, reason="keyword_miss")
    if cfg.min_changed_words and not hits:  # a keyword hit skips the word threshold
        basis = anchor_diff if (uses_anchor(cfg) and anchor_diff is not None) else latest_diff
        if threshold_words(basis, cfg) < cfg.min_changed_words:
            return GateVerdict(alert=False, reason="below_threshold")
    return GateVerdict(alert=True, keyword_hits=hits)
