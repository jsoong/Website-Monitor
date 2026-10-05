"""Operations on changes and on candidate filter configurations (nothing here stores a filter:
the user confirms by patching the bookmark)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from pagewatch.engine.bookmarks import InvalidError, NotFoundError
from pagewatch.engine.clock import iso
from pagewatch.engine.config import Resolved, resolve, resolve_candidate, source_options
from pagewatch.engine.fetch.base import FetchRequest, FetchResult
from pagewatch.engine.fetch.select import BROWSER, route_for
from pagewatch.engine.pipeline.autofilter import ProposeJob, propose_job
from pagewatch.engine.pipeline.core import (
    PreviewJob,
    RenderJob,
    RenderResult,
    TestFilterJob,
    ViewDiffJob,
    ViewVersion,
    compute_view_diff,
    preview_analyze,
    render_view,
    test_filter,
)
from pagewatch.engine.store import repo
from pagewatch.models import (
    ActionsConfig,
    FalsePositiveOut,
    FetchConfig,
    FilterConfig,
    GateConfig,
    PreviewOut,
    PreviewRequest,
    ProposalOut,
    RenderOut,
    TestFilterOut,
    TestFilterRequest,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine


class ConflictError(RuntimeError):
    """The request is valid but cannot be answered yet (e.g. only one version exists)."""


async def run_test_filter(
    engine: Engine, bookmark_id: int, req: TestFilterRequest
) -> TestFilterOut:
    def load(conn: sqlite3.Connection) -> tuple[Any, Any, Any]:
        row = repo.bookmark_get(conn, bookmark_id)
        if row is None:
            return None, None, None
        old_id = req.from_version_id or row["baseline_version_id"]
        new_id = req.to_version_id or row["latest_version_id"]
        old, new = repo.version_get(conn, old_id), repo.version_get(conn, new_id)
        if old is not None and old["bookmark_id"] != bookmark_id:
            old = None
        if new is not None and new["bookmark_id"] != bookmark_id:
            new = None
        return row, old, new

    row, old, new = await engine.db.read(load)
    if row is None:
        raise NotFoundError(f"bookmark {bookmark_id} not found")
    if old is None or new is None or not old["raw_hash"] or not new["raw_hash"]:
        raise ConflictError("this bookmark has no stored page to test against yet; check it first")
    patch: dict[str, dict[str, Any] | None] = {}
    if req.filter is not None:
        patch["filter"] = req.filter
    if req.gate is not None:
        patch["gate"] = req.gate
    try:
        resolved = resolve_candidate(row, patch, engine.folders, engine.settings)
    except ValidationError as exc:
        raise InvalidError(str(exc.errors()[0]["msg"])) from exc
    job = TestFilterJob(
        blob_root=str(engine.data_dir.blobs_dir),
        baseline_raw_hash=old["raw_hash"],
        baseline_content_type=old["content_type"] or "",
        latest_raw_hash=new["raw_hash"],
        latest_content_type=new["content_type"] or "",
        final_url=row["url"],
        source_type=row["source_type"],
        filter_cfg=resolved.filter.model_dump(mode="json"),
        gate_cfg=resolved.gate.model_dump(mode="json"),
        highlight_mode=(req.highlight_mode.value if req.highlight_mode else row["highlight_mode"]),
        source_cfg=source_options(resolved.fetch),
    )
    res = await engine.pool.run(test_filter, job)
    return TestFilterOut(
        baseline=res.baseline,
        latest=res.latest,
        marks=res.marks,
        diff=res.diff,
        alert=res.alert,
        reason=res.reason,
        keyword_hits=res.keyword_hits,
        identical=res.identical,
        warnings=res.warnings,
    )


async def flag_false_positive(engine: Engine, change_id: int) -> FalsePositiveOut:
    """Mark a change as a false positive and propose filters that would have prevented it."""

    def load(conn: sqlite3.Connection) -> tuple[Any, Any, Any, Any]:
        change = repo.change_get(conn, change_id)
        if change is None:
            return None, None, None, None
        row = repo.bookmark_get(conn, change["bookmark_id"])
        return (
            change,
            row,
            repo.version_get(conn, change["old_version_id"]),
            repo.version_get(conn, change["new_version_id"]),
        )

    change, row, old, new = await engine.db.read(load)
    if change is None or row is None:
        raise NotFoundError(f"change {change_id} not found")
    await engine.db.write(
        lambda c: c.execute("UPDATE change SET feedback='false_positive' WHERE id=?", (change_id,))
    )
    if old is None or new is None or not old["raw_hash"] or not new["raw_hash"]:
        raise ConflictError("there is no earlier version to compare this change with")
    resolved = resolve(row, engine.folders, engine.settings)
    result = await engine.pool.run(
        propose_job,
        ProposeJob(
            blob_root=str(engine.data_dir.blobs_dir),
            old_raw=old["raw_hash"],
            new_raw=new["raw_hash"],
            old_ctype=old["content_type"] or "",
            new_ctype=new["content_type"] or "",
            url=row["url"],
            source_type=row["source_type"],
            filter_cfg=resolved.filter.model_dump(mode="json"),
            highlight_mode=row["highlight_mode"],
            source_cfg=source_options(resolved.fetch),
        ),
    )
    proposals = [
        ProposalOut(
            rule=p.rule,
            kind=p.kind,
            pattern_name=p.pattern_name,
            explanation=p.explanation,
            example_old=p.example_old,
            example_new=p.example_new,
            verified=p.verified,
        )
        for p in result.proposals
    ]
    existing = list(resolved.overrides.get("filter", {}).get("ignore", []))
    return FalsePositiveOut(
        change_id=change_id,
        proposals=proposals,
        resolves_all=result.resolves_all,
        remaining_changed_blocks=result.remaining_changed_blocks,
        patch={"filter": {"ignore": [*existing, *(p.rule for p in result.proposals)]}},
    )


# -- rendering ----------------------------------------------------------------------------


def _view_version(row: Any) -> ViewVersion | None:
    if row is None:
        return None
    return ViewVersion(
        row["raw_hash"], row["content_type"] or "", row["blocks_hash"], row["screenshot_hash"]
    )


VIEWS = ("highlight", "text", "new", "old", "screenshot")


def _out(res: RenderResult) -> RenderOut:
    return RenderOut(html=res.html, view=res.view, identical=res.identical,
                     degraded=res.degraded, stats=res.stats)  # fmt: skip


async def render_change_raw(
    engine: Engine, change_id: int, view: str, *, allow_remote: bool, context: int | None
) -> RenderResult:
    """One change's own gate diff (the history view): old version -> new version. The worker's
    result, which also carries the picture for ``view=screenshot``."""

    def load(conn: sqlite3.Connection) -> tuple[Any, Any, Any, Any]:
        change = repo.change_get(conn, change_id)
        if change is None:
            return None, None, None, None
        row = repo.bookmark_get(conn, change["bookmark_id"])
        return (
            change,
            row,
            repo.version_get(conn, change["old_version_id"]),
            repo.version_get(conn, change["new_version_id"]),
        )

    change, row, old, new = await engine.db.read(load)
    if change is None or row is None or new is None:
        raise NotFoundError(f"change {change_id} not found")
    if view == "screenshot" and not new["screenshot_hash"]:
        raise NotFoundError("no screenshot was stored for this change")
    resolved = resolve(row, engine.folders, engine.settings)
    return await engine.pool.run(
        render_view,
        RenderJob(
            blob_root=str(engine.data_dir.blobs_dir),
            view=view,
            url=row["url"],
            new=_view_version(new),
            old=_view_version(old),
            diff_hash=change["diff_hash"],
            filter_cfg=resolved.filter.model_dump(mode="json"),
            highlight_mode=row["highlight_mode"],
            allow_remote=allow_remote,
            context=context,
            source_type=row["source_type"],
            source_cfg=source_options(resolved.fetch),
        ),
    )


async def render_change(
    engine: Engine, change_id: int, view: str, *, allow_remote: bool, context: int | None
) -> RenderOut:
    return _out(
        await render_change_raw(engine, change_id, view, allow_remote=allow_remote, context=context)
    )


async def render_unread_raw(
    engine: Engine, bookmark_id: int, view: str, *, allow_remote: bool, context: int | None
) -> RenderResult:
    """The viewer's default: everything unread, baseline -> latest. The text diff is computed in
    the worker pool and cached (one row per bookmark, replaced when either pointer moves); a
    screenshot comparison is cheap and is computed on demand."""
    if view not in ("highlight", "text", "screenshot"):
        raise InvalidError("the unread diff supports view=highlight, text or screenshot")

    def load(conn: sqlite3.Connection) -> tuple[Any, Any, Any, Any]:
        row = repo.bookmark_get(conn, bookmark_id)
        if row is None:
            return None, None, None, None
        cache = conn.execute(
            "SELECT * FROM view_diff_cache WHERE bookmark_id=?", (bookmark_id,)
        ).fetchone()
        return (
            row,
            repo.version_get(conn, row["baseline_version_id"]),
            repo.version_get(conn, row["latest_version_id"]),
            cache,
        )

    row, baseline, latest, cache = await engine.db.read(load)
    if row is None:
        raise NotFoundError(f"bookmark {bookmark_id} not found")
    if latest is None:
        raise ConflictError("this bookmark has not been checked yet")
    resolved = resolve(row, engine.folders, engine.settings)
    base = baseline if baseline is not None else latest
    diff_hash: str | None = None
    if view == "screenshot":
        if latest["screenshot_hash"] is None:
            raise NotFoundError("no screenshot was stored for this bookmark")
    elif base["id"] != latest["id"]:
        if (
            cache is not None
            and cache["baseline_version_id"] == base["id"]
            and cache["latest_version_id"] == latest["id"]
        ):
            diff_hash = cache["diff_hash"]
        else:
            vd = await engine.pool.run(
                compute_view_diff,
                ViewDiffJob(
                    blob_root=str(engine.data_dir.blobs_dir),
                    old_blocks_hash=base["blocks_hash"],
                    new_blocks_hash=latest["blocks_hash"],
                    ignore_case=resolved.filter.special.ignore_case,
                    highlight_mode=row["highlight_mode"],
                ),
            )
            diff_hash = vd.diff_hash
            now = iso(engine.clock.now())
            baseline_id = baseline["id"] if baseline is not None else None

            def store(conn: sqlite3.Connection) -> None:
                current = repo.bookmark_get(conn, bookmark_id)
                if (
                    current is not None
                    and current["latest_version_id"] == latest["id"]
                    and current["baseline_version_id"] == baseline_id
                ):  # the pointers have not moved while we computed
                    conn.execute(
                        "INSERT OR REPLACE INTO view_diff_cache(bookmark_id, baseline_version_id, "
                        "latest_version_id, diff_hash, created_at) VALUES(?,?,?,?,?)",
                        (bookmark_id, base["id"], latest["id"], vd.diff_hash, now),
                    )

            await engine.db.write(store)
    return await engine.pool.run(
        render_view,
        RenderJob(
            blob_root=str(engine.data_dir.blobs_dir),
            view=view,
            url=row["url"],
            new=_view_version(latest),
            old=_view_version(base),
            diff_hash=diff_hash,
            filter_cfg=resolved.filter.model_dump(mode="json"),
            highlight_mode=row["highlight_mode"],
            allow_remote=allow_remote,
            context=context,
            source_type=row["source_type"],
            source_cfg=source_options(resolved.fetch),
        ),
    )


async def render_unread(
    engine: Engine, bookmark_id: int, view: str, *, allow_remote: bool, context: int | None
) -> RenderOut:
    return _out(
        await render_unread_raw(
            engine, bookmark_id, view, allow_remote=allow_remote, context=context
        )
    )


async def render_version(
    engine: Engine, bookmark_id: int, which: str, *, allow_remote: bool
) -> RenderOut:
    """The latest ("new") or last-read ("old") stored page, unmarked."""

    def load(conn: sqlite3.Connection) -> tuple[Any, Any, Any]:
        row = repo.bookmark_get(conn, bookmark_id)
        if row is None:
            return None, None, None
        return (
            row,
            repo.version_get(conn, row["baseline_version_id"]),
            repo.version_get(conn, row["latest_version_id"]),
        )

    row, baseline, latest = await engine.db.read(load)
    if row is None:
        raise NotFoundError(f"bookmark {bookmark_id} not found")
    version = latest if which == "new" else baseline
    if version is None:
        raise ConflictError("this bookmark has not been checked yet")
    resolved = resolve(row, engine.folders, engine.settings)
    res = await engine.pool.run(
        render_view,
        RenderJob(
            blob_root=str(engine.data_dir.blobs_dir),
            view=which,
            url=row["url"],
            new=_view_version(version) if which == "new" else None,
            old=_view_version(version) if which == "old" else None,
            allow_remote=allow_remote,
            source_type=row["source_type"],
            source_cfg=source_options(resolved.fetch),
        ),
    )
    return RenderOut(html=res.html, view=res.view)


# -- add-bookmark preview -----------------------------------------------------------------


async def run_preview(engine: Engine, req: PreviewRequest) -> PreviewOut:
    """Trial fetch(es) of a URL with the given options. Nothing is stored.

    With ``check_method=auto`` a web page that shows (almost) nothing to a static fetch is fetched
    again in the browser and the rendered page is what the assistant shows, exactly as the first
    check of a saved bookmark would do (spec: Add-bookmark assistant, steps 1-2)."""
    try:
        fetch_cfg = FetchConfig.model_validate(req.fetch or {})
    except ValidationError as exc:
        raise InvalidError(str(exc.errors()[0]["msg"])) from exc
    if req.source_type.value == "records" and fetch_cfg.records is None:
        raise InvalidError("a records source needs a 'records' configuration")
    route = route_for(req.url, req.source_type.value, req.check_method.value, fetch_cfg)
    if route is None:
        raise InvalidError("unsupported url scheme")
    resolved = Resolved(
        schedule=engine.settings.default_schedule,
        fetch=fetch_cfg,
        filter=FilterConfig(),
        gate=GateConfig(),
        actions=ActionsConfig(),
        overrides={},
    )
    source_cfg = source_options(fetch_cfg)
    elapsed = 0

    async def sample(fetcher_route: Any) -> tuple[list[tuple[bytes, str]], FetchResult | None, str]:
        nonlocal elapsed
        samples: list[tuple[bytes, str]] = []
        first: FetchResult | None = None
        for n in range(req.samples):
            if n:
                await engine.clock.sleep(req.gap_s)
            result = await engine.fetchers.get(fetcher_route).fetch(
                FetchRequest(url=req.url, resolved=resolved, settings=engine.settings, force=True)
            )
            elapsed += result.elapsed_ms
            if result.error is not None:
                return [], result, f"{result.error.reason}: {result.error.message}"
            first = first or result
            samples.append((result.body, result.content_type))
        return samples, first, ""

    def failed(result: FetchResult | None, message: str, method: str) -> PreviewOut:
        return PreviewOut(
            final_url=result.final_url if result else req.url,
            status=result.status if result else None,
            content_type="", kind="error", method=method, js_app=False, readable_chars=0,
            words=0, blocks=0, html="", error=message, elapsed_ms=elapsed,
        )  # fmt: skip

    async def analyze(samples: list[tuple[bytes, str]], first: FetchResult) -> Any:
        return await engine.pool.run(
            preview_analyze,
            PreviewJob(samples, first.final_url, req.source_type.value,
                       FilterConfig().model_dump(mode="json"), source_cfg),
        )  # fmt: skip

    method = route.name
    samples, first, error = await sample(route)
    if error or first is None:
        return failed(first, error, method)
    analysis = await analyze(samples, first)
    warnings: list[str] = list(analysis.warnings)
    js_app = bool(analysis.js_app)
    if (
        req.check_method.value == "auto"
        and route.name == "static"
        and req.source_type.value in ("auto", "html")
        and analysis.js_app
    ):
        method = "browser"
        b_samples, b_first, b_error = await sample(BROWSER)
        if b_error or b_first is None:
            warnings.append(f"this page needs a browser, but one could not be used: {b_error}")
        else:
            samples, first = b_samples, b_first
            analysis = await analyze(samples, first)
            warnings = [*analysis.warnings, "rendered in the browser"]
    proposals = [
        ProposalOut(
            rule=p.rule, kind=p.kind, pattern_name=p.pattern_name, explanation=p.explanation,
            example_old=p.example_old, example_new=p.example_new, verified=p.verified,
        )
        for p in analysis.proposals
    ]  # fmt: skip
    return PreviewOut(
        final_url=first.final_url,
        status=first.status,
        content_type=first.content_type,
        kind=analysis.kind,
        method=method,
        js_app=js_app,
        readable_chars=analysis.chars,
        words=analysis.words,
        blocks=analysis.blocks,
        html=analysis.html,
        unstable_blocks=analysis.unstable_blocks,
        proposals=proposals,
        warnings=warnings,
        elapsed_ms=elapsed,
    )
