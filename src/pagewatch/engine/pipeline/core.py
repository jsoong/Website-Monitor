"""The CPU-bound half of a check, run in a worker process.

``process_check`` takes raw bytes plus the references to earlier versions, does everything
up to and including the gate verdict, writes the blobs the new version needs, and returns a
small picklable result. The event loop then commits it in one database transaction.

Check sequence (spec: Change detection and diffing):
  1. raw hash equals the latest version's  -> unchanged, stop before parsing
  2. normalise + filter; filtered hash equals the latest version's -> unchanged, stop
  3. bad-fetch gate rules (too short / blacklist / whitelist miss) -> rejected, nothing stored
  4. diff latest -> new (and anchor -> new for cumulative thresholds), run the gate
  5. write blobs for the version; the caller stores it and advances ``latest``
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pagewatch.engine.pipeline import gate as gate_mod
from pagewatch.engine.pipeline.diff import DiffResult, count_words, diff_blocks
from pagewatch.engine.pipeline.extract import (
    Block,
    blocks_from_json,
    blocks_to_json,
    decode_body,
    extract_blocks,
    json_blocks,
    parse_html,
    text_blocks,
)
from pagewatch.engine.pipeline.special import apply_special, comparison_text
from pagewatch.engine.store.blobs import BlobStore, sha256_hex, sha256_text
from pagewatch.models import FilterConfig, GateConfig, HighlightMode

Kind = Literal["unchanged_raw", "unchanged", "rejected", "first", "stored"]
SUMMARY_CHARS = 200


@dataclass(slots=True)
class VersionRef:
    version_id: int
    raw_hash: str | None
    blocks_hash: str
    filtered_hash: str


@dataclass(slots=True)
class PipelineJob:
    blob_root: str
    body: bytes
    content_type: str
    final_url: str
    source_type: str
    filter_cfg: dict[str, Any]
    gate_cfg: dict[str, Any]
    highlight_mode: str
    latest: VersionRef | None
    anchor: VersionRef | None


@dataclass(slots=True)
class PipelineResult:
    kind: Kind
    raw_hash: str
    blocks_hash: str | None = None
    filtered_hash: str | None = None
    word_count: int = 0
    chars: int = 0
    changed: bool = False  # the filtered text differs from the latest version's
    store_version: bool = False
    alert: bool = False
    reason: str | None = None
    keyword_hits: list[str] = field(default_factory=list)
    stats: dict[str, int] | None = None
    diff_hash: str | None = None
    summary: str | None = None
    compared_with: int | None = None  # version id the alert's diff starts from
    anchor_based: bool = False
    degraded: bool = False
    elapsed_ms: int = 0


# -- source type ------------------------------------------------------------------------


def resolve_kind(source_type: str, content_type: str, url: str, body: bytes) -> str:
    """``html`` / ``json`` / ``text`` / ``binary``: how the bytes become blocks."""
    ct = content_type.split(";")[0].strip().lower()
    if source_type in ("html",):
        return "html"
    if source_type == "binary":
        return "binary"
    if "html" in ct or "xhtml" in ct:
        return "html"
    if ct in ("application/json",) or ct.endswith("+json"):
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


# -- block building ---------------------------------------------------------------------


def build_blocks(
    body: bytes,
    content_type: str,
    final_url: str,
    source_type: str,
    cfg: FilterConfig,
    raw_hash: str,
) -> list[Block]:
    """Raw bytes -> the final filtered block list that is stored and compared."""
    special = cfg.special
    kind = resolve_kind(source_type, content_type, final_url, body)
    if kind == "binary":
        blocks = [Block("", "binary", f"binary content {len(body)} bytes sha256 {raw_hash}")]
        return apply_special(blocks, special)
    text = decode_body(body, content_type)
    if kind == "json":
        blocks = json_blocks(text, nfkc=special.normalize_unicode)
    elif kind == "text":
        blocks = text_blocks(text, nfkc=special.normalize_unicode)
    elif not special.text_only:  # compare the raw HTML source, line by line
        blocks = text_blocks(text, kind="source", nfkc=special.normalize_unicode)
    else:
        root = parse_html(text)
        blocks = extract_blocks(
            root,
            base_url=final_url,
            ignore_options=special.ignore_options,
            nfkc=special.normalize_unicode,
        )
    return apply_special(blocks, special)


def _texts(blocks: list[Block]) -> list[str]:
    return [b.text for b in blocks]


def _load(store: BlobStore, blocks_hash: str) -> list[Block]:
    return blocks_from_json(store.get_json(blocks_hash))


def _summary(text: str) -> str:
    text = " ".join(text.split())
    return text[:SUMMARY_CHARS]


# -- the check --------------------------------------------------------------------------


def process_check(job: PipelineJob) -> PipelineResult:
    started = time.perf_counter()
    store = BlobStore(Path(job.blob_root))
    raw_hash = sha256_hex(job.body)

    def done(res: PipelineResult) -> PipelineResult:
        res.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return res

    latest = job.latest
    if latest is not None and latest.raw_hash == raw_hash:
        return done(PipelineResult("unchanged_raw", raw_hash))

    fcfg = FilterConfig.model_validate(job.filter_cfg)
    gcfg = GateConfig.model_validate(job.gate_cfg)
    ignore_case = fcfg.special.ignore_case
    mode = HighlightMode(job.highlight_mode)

    blocks = build_blocks(
        job.body, job.content_type, job.final_url, job.source_type, fcfg, raw_hash
    )
    new_texts = _texts(blocks)
    filtered_hash = sha256_text(comparison_text(blocks, fcfg.special))
    chars = sum(len(t) for t in new_texts)

    if latest is not None and latest.filtered_hash == filtered_hash:
        return done(PipelineResult("unchanged", raw_hash, filtered_hash=filtered_hash, chars=chars))

    # Bad fetches (error page, near-empty page, blacklisted/whitelist-less) are rejected
    # before anything is stored, so they can never become the comparison base.
    bad = gate_mod.check_bad_fetch(gcfg, new_texts)
    if bad is not None:
        return done(
            PipelineResult(
                "rejected", raw_hash, filtered_hash=filtered_hash, chars=chars, reason=bad.reason
            )
        )

    words = sum(count_words(t) for t in new_texts)
    store.put_with_digest(raw_hash, job.body)
    blocks_hash = store.put_json(blocks_to_json(blocks))

    if latest is None:
        return done(
            PipelineResult(
                "first",
                raw_hash,
                blocks_hash,
                filtered_hash,
                words,
                chars,
                changed=False,
                store_version=True,
                summary=_summary(" ".join(new_texts)),
            )
        )

    old_blocks = _load(store, latest.blocks_hash)
    latest_diff = diff_blocks(
        _texts(old_blocks), new_texts, ignore_case=ignore_case, detect_moves=_moves(mode)
    )
    anchor_diff: DiffResult | None = None
    anchor = job.anchor
    if gate_mod.uses_anchor(gcfg) and anchor is not None and anchor.version_id != latest.version_id:
        anchor_blocks = _load(store, anchor.blocks_hash)
        anchor_diff = diff_blocks(
            _texts(anchor_blocks), new_texts, ignore_case=ignore_case, detect_moves=_moves(mode)
        )

    verdict = gate_mod.check_change(gcfg, latest_diff=latest_diff, anchor_diff=anchor_diff)
    res = PipelineResult(
        "stored",
        raw_hash,
        blocks_hash,
        filtered_hash,
        words,
        chars,
        changed=True,
        store_version=True,
        alert=verdict.alert,
        reason=verdict.reason,
        keyword_hits=verdict.keyword_hits,
        degraded=latest_diff.degraded or (anchor_diff.degraded if anchor_diff else False),
    )
    if verdict.alert:
        report = anchor_diff if anchor_diff is not None else latest_diff
        res.anchor_based = anchor_diff is not None
        res.compared_with = (
            anchor.version_id if anchor_diff is not None and anchor else latest.version_id
        )
        res.stats = {
            "added_words": report.added_words,
            "removed_words": report.removed_words,
            "changed_blocks": report.changed_blocks,
        }
        res.diff_hash = store.put_json(report.to_json())
        res.summary = _summary(report.summary_text(new_texts) or " ".join(new_texts))
    return done(res)


def _moves(mode: HighlightMode) -> bool:
    return mode is not HighlightMode.EXACT  # moves count as changes in Exact mode


# -- other worker jobs ------------------------------------------------------------------


@dataclass(slots=True)
class RebuildJob:
    blob_root: str
    raw_hash: str
    content_type: str
    final_url: str
    source_type: str
    filter_cfg: dict[str, Any]


@dataclass(slots=True)
class RebuildResult:
    blocks_hash: str
    filtered_hash: str
    word_count: int


def rebuild_version(job: RebuildJob) -> RebuildResult:
    """Re-run normalisation on a stored raw blob with the current filter config."""
    store = BlobStore(Path(job.blob_root))
    body = store.get(job.raw_hash)
    fcfg = FilterConfig.model_validate(job.filter_cfg)
    blocks = build_blocks(
        body, job.content_type, job.final_url, job.source_type, fcfg, job.raw_hash
    )
    texts = _texts(blocks)
    return RebuildResult(
        store.put_json(blocks_to_json(blocks)),
        sha256_text(comparison_text(blocks, fcfg.special)),
        sum(count_words(t) for t in texts),
    )


@dataclass(slots=True)
class ViewDiffJob:
    blob_root: str
    old_blocks_hash: str
    new_blocks_hash: str
    ignore_case: bool
    highlight_mode: str


@dataclass(slots=True)
class ViewDiffResult:
    diff_hash: str
    stats: dict[str, int]
    degraded: bool


def compute_view_diff(job: ViewDiffJob) -> ViewDiffResult:
    """The viewer's default diff: last-read version -> latest."""
    store = BlobStore(Path(job.blob_root))
    old, new = _load(store, job.old_blocks_hash), _load(store, job.new_blocks_hash)
    diff = diff_blocks(
        _texts(old),
        _texts(new),
        ignore_case=job.ignore_case,
        detect_moves=_moves(HighlightMode(job.highlight_mode)),
    )
    return ViewDiffResult(store.put_json(diff.to_json()), diff.stats, diff.degraded)
