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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pagewatch.engine.pipeline import filters
from pagewatch.engine.pipeline import gate as gate_mod
from pagewatch.engine.pipeline import keywords as kw
from pagewatch.engine.pipeline.diff import DiffResult, count_words, diff_blocks
from pagewatch.engine.pipeline.extract import (
    Block,
    blocks_from_json,
    blocks_to_json,
    decode_body,
    json_blocks,
    parse_html,
    text_blocks,
)
from pagewatch.engine.pipeline.render import render_marks
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
    warnings: list[str] = field(default_factory=list)


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
    warn: Callable[[str], None] | None = None,
) -> list[Block]:
    """Raw bytes -> the final filtered block list that is stored and compared.

    Order (spec): parse -> cosmetic -> block extraction -> watch -> ignore -> special."""
    warn = warn or (lambda _msg: None)
    special = cfg.special
    kind = resolve_kind(source_type, content_type, final_url, body)
    if kind == "binary":
        blocks = [Block("", "binary", f"binary content {len(body)} bytes sha256 {raw_hash}")]
        return apply_special(blocks, special)
    text = decode_body(body, content_type)
    root: Any = None
    if kind == "json":
        blocks = json_blocks(text, nfkc=special.normalize_unicode)
    elif kind == "text":
        blocks = text_blocks(text, nfkc=special.normalize_unicode)
    elif not special.text_only:  # compare the raw HTML source, line by line
        blocks = text_blocks(text, kind="source", nfkc=special.normalize_unicode)
    else:
        root = parse_html(text)
        filters.mark_dom(root, cfg, warn)
        blocks = []
    if root is not None or kind == "html" and special.text_only:
        blocks = filters.collect_blocks(
            root,
            cfg,
            base_url=final_url,
            ignore_options=special.ignore_options,
            nfkc=special.normalize_unicode,
            warn=warn,
        )
    elif cfg.watch:  # no DOM: only marker/text watch rules can apply
        blocks = filters.collect_blocks(
            None,
            cfg,
            base_url=final_url,
            ignore_options=special.ignore_options,
            nfkc=special.normalize_unicode,
            warn=warn,
            page_blocks=blocks,
        )
    blocks = filters.apply_ignore(blocks, root, cfg, warn)
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

    warnings: list[str] = []
    blocks = build_blocks(
        job.body, job.content_type, job.final_url, job.source_type, fcfg, raw_hash, warnings.append
    )
    new_texts = _texts(blocks)
    filtered_hash = sha256_text(comparison_text(blocks, fcfg.special))
    chars = sum(len(t) for t in new_texts)

    if latest is not None and latest.filtered_hash == filtered_hash:
        return done(
            PipelineResult(
                "unchanged", raw_hash, filtered_hash=filtered_hash, chars=chars, warnings=warnings
            )
        )

    # Bad fetches (error page, near-empty page, blacklisted/whitelist-less) are rejected
    # before anything is stored, so they can never become the comparison base.
    bad = gate_mod.check_bad_fetch(gcfg, new_texts)
    if bad is not None:
        return done(
            PipelineResult(
                "rejected",
                raw_hash,
                filtered_hash=filtered_hash,
                chars=chars,
                reason=bad.reason,
                warnings=warnings,
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
                warnings=warnings,
            )
        )

    old_blocks = _load(store, latest.blocks_hash)
    latest_diff = diff_blocks(
        _texts(old_blocks),
        new_texts,
        ignore_case=ignore_case,
        detect_moves=_moves(mode),
        table=mode is HighlightMode.TABLE,
    )
    anchor_diff: DiffResult | None = None
    anchor = job.anchor
    if gate_mod.uses_anchor(gcfg) and anchor is not None and anchor.version_id != latest.version_id:
        anchor_blocks = _load(store, anchor.blocks_hash)
        anchor_diff = diff_blocks(
            _texts(anchor_blocks),
            new_texts,
            ignore_case=ignore_case,
            detect_moves=_moves(mode),
            table=mode is HighlightMode.TABLE,
        )

    # Keywords look at the changes since the *latest* version; page() terms at the whole page.
    rules = kw.parse_rules(gcfg.keywords)
    changes = latest_diff.change_set(new_texts)
    page_text = "\n".join(new_texts)
    hits = kw.evaluate(rules, page_text=page_text, changes=changes)
    shown = hits + [
        h
        for h in kw.evaluate(
            kw.parse_rules(gcfg.highlight_keywords), page_text=page_text, changes=changes
        )
        if h not in hits
    ]
    verdict = gate_mod.check_change(
        gcfg,
        latest_diff=latest_diff,
        anchor_diff=anchor_diff,
        keywords_configured=bool(rules),
        keyword_hits=hits,
    )
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
        keyword_hits=shown,
        degraded=latest_diff.degraded or (anchor_diff.degraded if anchor_diff else False),
        warnings=warnings,
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
    mode = HighlightMode(job.highlight_mode)
    diff = diff_blocks(
        _texts(old),
        _texts(new),
        ignore_case=job.ignore_case,
        detect_moves=_moves(mode),
        table=mode is HighlightMode.TABLE,
    )
    return ViewDiffResult(store.put_json(diff.to_json()), diff.stats, diff.degraded)


@dataclass(slots=True)
class TestFilterJob:
    """Run the full pipeline over two stored raw versions with a *candidate* configuration."""

    __test__ = False  # not a pytest test class

    blob_root: str
    baseline_raw_hash: str
    baseline_content_type: str
    latest_raw_hash: str
    latest_content_type: str
    final_url: str
    source_type: str
    filter_cfg: dict[str, Any]
    gate_cfg: dict[str, Any]
    highlight_mode: str


@dataclass(slots=True)
class TestFilterResult:
    __test__ = False

    baseline: list[str]
    latest: list[str]
    marks: list[str]
    diff: dict[str, Any]
    alert: bool
    reason: str | None
    keyword_hits: list[str]
    identical: bool
    warnings: list[str] = field(default_factory=list)


def test_filter(job: TestFilterJob) -> TestFilterResult:
    """What the gate would have done if ``latest`` had just been fetched on top of ``baseline``.
    Reads blobs only; nothing is stored."""
    store = BlobStore(Path(job.blob_root))
    fcfg = FilterConfig.model_validate(job.filter_cfg)
    gcfg = GateConfig.model_validate(job.gate_cfg)
    mode = HighlightMode(job.highlight_mode)
    warnings: list[str] = []

    def blocks_of(raw_hash: str, ctype: str) -> list[str]:
        body = store.get(raw_hash)
        return _texts(
            build_blocks(
                body, ctype, job.final_url, job.source_type, fcfg, raw_hash, warnings.append
            )
        )

    old = blocks_of(job.baseline_raw_hash, job.baseline_content_type)
    new = (
        old
        if job.latest_raw_hash == job.baseline_raw_hash
        else blocks_of(job.latest_raw_hash, job.latest_content_type)
    )
    diff = diff_blocks(
        old,
        new,
        ignore_case=fcfg.special.ignore_case,
        detect_moves=_moves(mode),
        table=mode is HighlightMode.TABLE,
    )
    if diff.is_empty and old == new:
        return TestFilterResult(old, new, render_marks(diff, old, new), diff.to_json(), False,
                                "unchanged", [], True, warnings)  # fmt: skip
    bad = gate_mod.check_bad_fetch(gcfg, new)
    rules = kw.parse_rules(gcfg.keywords)
    hits = kw.evaluate(rules, page_text="\n".join(new), changes=diff.change_set(new))
    verdict = bad or gate_mod.check_change(
        gcfg, latest_diff=diff, anchor_diff=diff,
        keywords_configured=bool(rules), keyword_hits=hits,
    )  # fmt: skip
    return TestFilterResult(
        old, new, render_marks(diff, old, new), diff.to_json(), verdict.alert, verdict.reason,
        verdict.keyword_hits if not bad else [], False, warnings,
    )  # fmt: skip


# -- views --------------------------------------------------------------------------------


@dataclass(slots=True)
class ViewVersion:
    raw_hash: str | None
    content_type: str
    blocks_hash: str


@dataclass(slots=True)
class RenderJob:
    blob_root: str
    view: str  # highlight | text | new | old
    url: str
    new: ViewVersion | None
    old: ViewVersion | None
    diff_hash: str | None = None
    filter_cfg: dict[str, Any] = field(default_factory=dict)
    highlight_mode: str = "standard"
    allow_remote: bool = False
    context: int | None = None


@dataclass(slots=True)
class RenderResult:
    html: str
    view: str  # the view actually produced (highlight falls back to text)
    identical: bool = False
    degraded: bool = False
    stats: dict[str, int] = field(default_factory=dict)


def render_view(job: RenderJob) -> RenderResult:
    """Render a stored version or the diff between two stored versions as a sanitised
    HTML document. Reads blobs only."""
    from pagewatch.engine.pipeline import viewer

    store = BlobStore(Path(job.blob_root))
    if job.view in ("new", "old"):
        v = job.new if job.view == "new" else job.old
        if v is None or not v.raw_hash:
            return RenderResult(
                viewer.wrap_document('<div class="pw-note">No stored copy of this version.</div>'),
                job.view,
            )
        plain = viewer.render_plain(
            store.get(v.raw_hash), v.content_type, job.url, allow_remote=job.allow_remote
        )
        return RenderResult(
            viewer.wrap_document(plain, allow_remote=job.allow_remote, base_url=job.url), job.view
        )

    assert job.new is not None
    fcfg = FilterConfig.model_validate(job.filter_cfg)
    new_blocks = _load(store, job.new.blocks_hash)
    old_blocks = _load(store, job.old.blocks_hash) if job.old is not None else []
    new_texts, old_texts = _texts(new_blocks), _texts(old_blocks)
    if job.diff_hash:
        diff = DiffResult.from_json(store.get_json(job.diff_hash))
    else:
        mode = HighlightMode(job.highlight_mode)
        diff = diff_blocks(
            old_texts, new_texts, ignore_case=fcfg.special.ignore_case,
            detect_moves=_moves(mode), table=mode is HighlightMode.TABLE,
        )  # fmt: skip
    identical = job.old is not None and old_texts == new_texts
    produced = "text"
    body: str | None = None
    if job.view == "highlight" and job.new.raw_hash:
        body = viewer.render_highlight(
            store.get(job.new.raw_hash), job.new.content_type, job.url, new_blocks, old_texts,
            diff, nfkc=fcfg.special.normalize_unicode, ignore_options=fcfg.special.ignore_options,
            allow_remote=job.allow_remote,
        )  # fmt: skip
        if body is not None:
            produced = "highlight"
    if body is None:
        body = viewer.render_text(diff, old_texts, new_texts, context=job.context)
    if identical:
        body = (
            '<div class="pw-note">No unread changes: this is the last page you read.</div>' + body
        )
    return RenderResult(
        viewer.wrap_document(body, allow_remote=job.allow_remote, base_url=job.url),
        produced,
        identical,
        diff.degraded,
        diff.stats,
    )


# -- preview ------------------------------------------------------------------------------


@dataclass(slots=True)
class PreviewJob:
    samples: list[tuple[bytes, str]]  # (body, content type), fetched `gap` apart
    url: str
    source_type: str
    filter_cfg: dict[str, Any]


@dataclass(slots=True)
class PreviewAnalysis:
    kind: str
    js_app: bool
    chars: int
    words: int
    blocks: int
    html: str
    unstable_blocks: int
    proposals: list[Any]
    warnings: list[str]


def preview_analyze(job: PreviewJob) -> PreviewAnalysis:
    """What the add-bookmark assistant shows: type, size, a rendered page, and (given two
    samples) which blocks already differ between fetches, with proposed ignore filters."""
    from pagewatch.engine.pipeline import autofilter, detect, viewer

    fcfg = FilterConfig.model_validate(job.filter_cfg)
    warnings: list[str] = []
    body, ctype = job.samples[0]
    raw_hash = sha256_hex(body)
    blocks = build_blocks(body, ctype, job.url, job.source_type, fcfg, raw_hash, warnings.append)
    texts = _texts(blocks)
    chars = sum(len(t) for t in texts)
    kind = detect.classify(ctype, body, chars)
    js_app = kind == "js-app"
    html = viewer.wrap_document(viewer.render_plain(body, ctype, job.url), base_url=job.url)

    unstable = 0
    proposals: list[Any] = []
    if len(job.samples) > 1:
        mem = autofilter.MemoryBlobStore()
        first = mem.put(body)
        for other, other_type in job.samples[1:]:
            other_blocks = _texts(
                build_blocks(
                    other,
                    other_type,
                    job.url,
                    job.source_type,
                    fcfg,
                    sha256_hex(other),
                    warnings.append,
                )
            )
            diff = diff_blocks(texts, other_blocks, ignore_case=fcfg.special.ignore_case)
            if diff.is_empty:
                continue
            unstable += diff.changed_blocks
            result = autofilter.propose(
                mem,
                old_raw=first,
                new_raw=mem.put(other),
                old_ctype=ctype,
                new_ctype=other_type,
                url=job.url,
                source_type=job.source_type,
                filter_cfg=job.filter_cfg,
                highlight_mode="standard",
            )
            proposals.extend(result.proposals)
    return PreviewAnalysis(
        kind, js_app, chars, sum(count_words(t) for t in texts), len(blocks), html, unstable,
        proposals, warnings,
    )  # fmt: skip
