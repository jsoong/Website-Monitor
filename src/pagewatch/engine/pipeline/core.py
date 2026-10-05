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

from pagewatch.engine.pipeline import detect, filters, records, sources
from pagewatch.engine.pipeline import gate as gate_mod
from pagewatch.engine.pipeline import keywords as kw
from pagewatch.engine.pipeline import screenshot as shot_mod
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
from pagewatch.engine.pipeline.sources import resolve_kind
from pagewatch.engine.pipeline.special import apply_special, comparison_text
from pagewatch.engine.store.blobs import BlobStore, sha256_hex, sha256_text
from pagewatch.models import FilterConfig, GateConfig, HighlightMode, RecordsConfig

Kind = Literal["unchanged_raw", "unchanged", "rejected", "first", "stored", "needs_browser"]
SUMMARY_CHARS = 200


@dataclass(slots=True)
class VersionRef:
    version_id: int
    raw_hash: str | None
    blocks_hash: str
    filtered_hash: str
    screenshot_hash: str | None = None


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
    source_cfg: dict[str, Any] = field(default_factory=dict)  # fetch.records / fetch.feed
    screenshot_png: bytes | None = None  # the screenshot method: compare pictures, not text
    detect_js: bool = False  # method auto-detection: does a static fetch show (almost) nothing?


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
    source_kind: str = "html"  # what resolve_kind made of the bytes
    screenshot_hash: str | None = None
    needs_browser: bool = False  # auto-detection: re-run this check in the browser


# -- block building ---------------------------------------------------------------------


def records_config(source_cfg: dict[str, Any] | None) -> RecordsConfig:
    raw = (source_cfg or {}).get("records")
    if not raw:
        raise records.RecordsError("a records source needs a 'records' configuration")
    return RecordsConfig.model_validate(raw)


def build_blocks(
    body: bytes,
    content_type: str,
    final_url: str,
    source_type: str,
    cfg: FilterConfig,
    raw_hash: str,
    warn: Callable[[str], None] | None = None,
    source_cfg: dict[str, Any] | None = None,
) -> list[Block]:
    """Raw bytes -> the final filtered block list that is stored and compared.

    Order (spec): parse -> cosmetic -> block extraction -> watch -> ignore -> special.
    Documents and feeds are converted to HTML first ("they already arrive as HTML")."""
    warn = warn or (lambda _msg: None)
    special = cfg.special
    kind = resolve_kind(source_type, content_type, final_url, body)
    if kind == "binary":
        blocks = [Block("", "binary", f"binary content {len(body)} bytes sha256 {raw_hash}")]
        return apply_special(blocks, special)
    if kind == "image":
        blocks = [Block("", "image", sources.describe_image(body, raw_hash))]
        return apply_special(blocks, special)
    if kind == "records":
        rcfg = records_config(source_cfg)
        rows = records.load_rows(
            decode_body(body, content_type), rcfg, content_type=content_type, url=final_url,
            warn=warn,
        )  # fmt: skip
        blocks = records.record_blocks(rows, rcfg, nfkc=special.normalize_unicode, warn=warn)
        if cfg.watch:  # no DOM: only marker/text watch rules can apply
            blocks = filters.collect_blocks(
                None, cfg, base_url=final_url, ignore_options=special.ignore_options,
                nfkc=special.normalize_unicode, warn=warn, page_blocks=blocks,
            )  # fmt: skip
        blocks = filters.apply_ignore(blocks, None, cfg, warn)
        return apply_special(blocks, special)
    if kind in sources.CONVERTED_KINDS:
        text = sources.to_html(kind, body, source_cfg, warn)
        kind = "html"
    else:
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


def _browser_reason(job: PipelineJob, raw_hash: str) -> str | None:
    """Method auto-detection: why this static fetch should be re-run in the browser, if it should.
    Looks at the *unfiltered* page, so a watch filter cannot make a normal page look empty."""
    text = decode_body(job.body, job.content_type)
    plain = build_blocks(
        job.body, job.content_type, job.final_url, job.source_type, FilterConfig(), raw_hash
    )
    return detect.browser_reason(text, sum(len(b.text) for b in plain))


def process_check(job: PipelineJob) -> PipelineResult:
    started = time.perf_counter()
    store = BlobStore(Path(job.blob_root))
    raw_hash = sha256_hex(job.body)
    kind = resolve_kind(job.source_type, job.content_type, job.final_url, job.body)
    shot = job.screenshot_png
    shot_hash = sha256_hex(shot) if shot is not None else None

    def done(res: PipelineResult) -> PipelineResult:
        res.elapsed_ms = int((time.perf_counter() - started) * 1000)
        res.source_kind = kind
        if res.store_version:
            res.screenshot_hash = shot_hash
        return res

    latest = job.latest
    if shot is None:
        if latest is not None and latest.raw_hash == raw_hash:
            return done(PipelineResult("unchanged_raw", raw_hash))
    elif latest is not None and latest.screenshot_hash == shot_hash:
        # the screenshot method compares pictures: identical pixels are no change, whatever
        # the markup did
        return done(PipelineResult("unchanged_raw", raw_hash))

    fcfg = FilterConfig.model_validate(job.filter_cfg)
    gcfg = GateConfig.model_validate(job.gate_cfg)
    ignore_case = fcfg.special.ignore_case
    mode = HighlightMode(job.highlight_mode)

    warnings: list[str] = []
    if job.detect_js and kind == "html":
        why = _browser_reason(job, raw_hash)
        if why is not None:
            return done(PipelineResult("needs_browser", raw_hash, reason=why, needs_browser=True))
    blocks = build_blocks(
        job.body, job.content_type, job.final_url, job.source_type, fcfg, raw_hash,
        warnings.append, job.source_cfg,
    )  # fmt: skip
    new_texts = _texts(blocks)
    filtered_hash = sha256_text(comparison_text(blocks, fcfg.special))
    chars = sum(len(t) for t in new_texts)

    if shot is None and latest is not None and latest.filtered_hash == filtered_hash:
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
    if shot is not None and shot_hash is not None:
        return done(
            _screenshot_check(
                job, store, shot, shot_hash, raw_hash, blocks, filtered_hash, words, chars,
                fcfg, warnings,
            )
        )  # fmt: skip

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
    anchor_blocks: list[Block] = []
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
    alert, reason = verdict.alert, verdict.reason
    events: records.RecordEvents | None = None
    if kind == "records":
        # Each records bookmark chooses which events alert (new / changed / removed).
        events = records.record_events(old_blocks, blocks, ignore_case=ignore_case)
        if alert and not (events.kinds() & set(records_config(job.source_cfg).events)):
            alert, reason = False, "records_events"
    res = PipelineResult(
        "stored",
        raw_hash,
        blocks_hash,
        filtered_hash,
        words,
        chars,
        changed=True,
        store_version=True,
        alert=alert,
        reason=reason,
        keyword_hits=shown,
        degraded=latest_diff.degraded or (anchor_diff.degraded if anchor_diff else False),
        warnings=warnings,
    )
    if alert:
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
        if events is not None and events:
            reference = anchor_blocks if (anchor_diff is not None and anchor_blocks) else old_blocks
            if reference is not old_blocks:
                events = records.record_events(reference, blocks, ignore_case=ignore_case)
            res.summary = _summary(records.describe_events(events, blocks, reference))
    return done(res)


def _screenshot_check(
    job: PipelineJob,
    store: BlobStore,
    png: bytes,
    shot_hash: str,
    raw_hash: str,
    blocks: list[Block],
    filtered_hash: str,
    words: int,
    chars: int,
    fcfg: FilterConfig,
    warnings: list[str],
) -> PipelineResult:
    """The screenshot method (spec: Screenshot comparison): the picture decides. Text rules
    (keywords, word thresholds, ignore-removed) do not apply; the bad-fetch rules already ran.
    The page text is still stored so the Text diff, New and Old tabs keep working."""
    latest = job.latest

    def store_all() -> str:
        store.put_with_digest(raw_hash, job.body)
        store.put_with_digest(shot_hash, png)
        return store.put_json(blocks_to_json(blocks))

    if latest is None:
        blocks_hash = store_all()
        return PipelineResult(
            "first", raw_hash, blocks_hash, filtered_hash, words, chars, store_version=True,
            summary="First screenshot stored", warnings=warnings,
        )  # fmt: skip
    if latest.screenshot_hash is None:
        # The bookmark switched to the screenshot method: there is no earlier picture to compare.
        blocks_hash = store_all()
        return PipelineResult(
            "stored", raw_hash, blocks_hash, filtered_hash, words, chars, changed=True,
            store_version=True, reason="screenshot_baseline", warnings=warnings,
        )  # fmt: skip
    cfg = fcfg.screenshot
    diff = shot_mod.compare(
        store.get(latest.screenshot_hash), png, ignore=cfg.ignore, min_ratio=cfg.min_ratio,
        height_change_pct=cfg.height_change_pct,
    )  # fmt: skip
    if not diff.significant:
        if not diff.identical:
            warnings.append(f"the screenshot differs by {diff.ratio:.3%}, below the threshold")
        return PipelineResult(
            "unchanged", raw_hash, filtered_hash=filtered_hash, chars=chars, warnings=warnings
        )
    blocks_hash = store_all()
    overlay_hash = store.put(shot_mod.overlay_png(png, diff, ignore=cfg.ignore))
    payload = {"type": "screenshot", "old": latest.screenshot_hash, "new": shot_hash,
               "overlay": overlay_hash, **diff.to_json()}  # fmt: skip
    return PipelineResult(
        "stored", raw_hash, blocks_hash, filtered_hash, words, chars, changed=True,
        store_version=True, alert=True, stats={"changed_blocks": diff.region_count},
        diff_hash=store.put_json(payload), summary=diff.summary(),
        compared_with=latest.version_id, warnings=warnings,
    )  # fmt: skip


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
    source_cfg: dict[str, Any] = field(default_factory=dict)


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
        body, job.content_type, job.final_url, job.source_type, fcfg, job.raw_hash,
        source_cfg=job.source_cfg,
    )  # fmt: skip
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
    source_cfg: dict[str, Any] = field(default_factory=dict)


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
                body,
                ctype,
                job.final_url,
                job.source_type,
                fcfg,
                raw_hash,
                warnings.append,
                job.source_cfg,
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
    screenshot_hash: str | None = None


@dataclass(slots=True)
class RenderJob:
    blob_root: str
    view: str  # highlight | text | new | old | screenshot
    url: str
    new: ViewVersion | None
    old: ViewVersion | None
    diff_hash: str | None = None
    filter_cfg: dict[str, Any] = field(default_factory=dict)
    highlight_mode: str = "standard"
    allow_remote: bool = False
    context: int | None = None
    source_type: str = "auto"
    source_cfg: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RenderResult:
    html: str
    view: str  # the view actually produced (highlight falls back to text)
    identical: bool = False
    degraded: bool = False
    stats: dict[str, int] = field(default_factory=dict)
    png: bytes | None = None  # the screenshot view: the overlay picture


def render_view(job: RenderJob) -> RenderResult:
    """Render a stored version or the diff between two stored versions as a sanitised
    HTML document. Reads blobs only."""
    from pagewatch.engine.pipeline import viewer

    store = BlobStore(Path(job.blob_root))
    if job.view == "screenshot":
        return render_screenshot(job, store)
    if job.view in ("new", "old"):
        v = job.new if job.view == "new" else job.old
        if v is None or not v.raw_hash:
            return RenderResult(
                viewer.wrap_document('<div class="pw-note">No stored copy of this version.</div>'),
                job.view,
            )
        raw, ctype = sources.view_bytes(
            store.get(v.raw_hash), v.content_type, job.url, job.source_type, job.source_cfg,
            plain=True,
        )  # fmt: skip
        plain = viewer.render_plain(raw, ctype, job.url, allow_remote=job.allow_remote)
        return RenderResult(
            viewer.wrap_document(plain, allow_remote=job.allow_remote, base_url=job.url), job.view
        )

    assert job.new is not None
    fcfg = FilterConfig.model_validate(job.filter_cfg)
    new_blocks = _load(store, job.new.blocks_hash)
    old_blocks = _load(store, job.old.blocks_hash) if job.old is not None else []
    new_texts, old_texts = _texts(new_blocks), _texts(old_blocks)
    diff_hash = job.diff_hash
    if diff_hash and store.get_json(diff_hash).get("type") == "screenshot":
        diff_hash = None  # a visual change's gate diff is a picture; the text diff is computed
    if diff_hash:
        diff = DiffResult.from_json(store.get_json(diff_hash))
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
        raw, ctype = sources.view_bytes(
            store.get(job.new.raw_hash), job.new.content_type, job.url, job.source_type,
            job.source_cfg,
        )  # fmt: skip
        body = viewer.render_highlight(
            raw, ctype, job.url, new_blocks, old_texts,
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


def render_screenshot(job: RenderJob, store: BlobStore) -> RenderResult:
    """The screenshot diff: the overlay (red boxes) of a change's own gate diff, or a fresh
    comparison of the old and new stored pictures (the viewer's unread diff). Reads blobs only."""
    from pagewatch.engine.pipeline import viewer

    fcfg = FilterConfig.model_validate(job.filter_cfg) if job.filter_cfg else FilterConfig()
    cfg = fcfg.screenshot
    new_hash = job.new.screenshot_hash if job.new else None
    old_hash = job.old.screenshot_hash if job.old else None
    if job.diff_hash:
        payload = store.get_json(job.diff_hash)
        if payload.get("type") == "screenshot" and store.exists(payload["overlay"]):
            diff = shot_mod.ShotDiff.from_json(payload)
            return _screenshot_result(viewer, diff, store.get(payload["overlay"]), False)
    if new_hash is None:
        return RenderResult(
            viewer.wrap_document(
                '<div class="pw-note">No screenshot was stored for this version.</div>'
            ),
            "screenshot",
        )
    new_png = store.get(new_hash)
    if old_hash is None or old_hash == new_hash:
        diff = shot_mod.ShotDiff(identical=True, height_old=0, height_new=0)
        identical = old_hash == new_hash
        return _screenshot_result(viewer, diff, new_png, identical, no_baseline=old_hash is None)
    diff = shot_mod.compare(
        store.get(old_hash), new_png, ignore=cfg.ignore, min_ratio=cfg.min_ratio,
        height_change_pct=cfg.height_change_pct,
    )  # fmt: skip
    overlay = shot_mod.overlay_png(new_png, diff, ignore=cfg.ignore)
    return _screenshot_result(viewer, diff, overlay, diff.identical)


def _screenshot_result(
    viewer: Any, diff: Any, png: bytes, identical: bool, *, no_baseline: bool = False
) -> RenderResult:
    import base64

    if no_baseline:
        note = "There is no earlier screenshot to compare with."
    elif identical:
        note = "No unread changes: this is the last screenshot you read."
    else:
        note = diff.summary()
    img = (
        '<img alt="screenshot diff" style="max-width:100%;height:auto" '
        f'src="data:image/png;base64,{base64.b64encode(png).decode()}">'
    )
    html = viewer.wrap_document(f'<div class="pw-note">{viewer._esc(note)}</div>{img}')
    return RenderResult(html, "screenshot", identical, False, diff.stats(), png)


# -- preview ------------------------------------------------------------------------------


@dataclass(slots=True)
class PreviewJob:
    samples: list[tuple[bytes, str]]  # (body, content type), fetched `gap` apart
    url: str
    source_type: str
    filter_cfg: dict[str, Any]
    source_cfg: dict[str, Any] = field(default_factory=dict)


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
    blocks = build_blocks(
        body, ctype, job.url, job.source_type, fcfg, raw_hash, warnings.append, job.source_cfg
    )
    texts = _texts(blocks)
    chars = sum(len(t) for t in texts)
    resolved = resolve_kind(job.source_type, ctype, job.url, body)
    kind = detect.classify(ctype, body, chars, resolved)
    js_app = kind == "js-app"
    view, view_type = sources.view_bytes(
        body, ctype, job.url, job.source_type, job.source_cfg, plain=True
    )
    html = viewer.wrap_document(viewer.render_plain(view, view_type, job.url), base_url=job.url)

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
                    job.source_cfg,
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
                source_cfg=job.source_cfg,
            )
            proposals.extend(result.proposals)
    return PreviewAnalysis(
        kind, js_app, chars, sum(count_words(t) for t in texts), len(blocks), html, unstable,
        proposals, warnings,
    )  # fmt: skip
