"""Operations on changes and on candidate filter configurations (nothing here stores a filter:
the user confirms by patching the bookmark)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from pagewatch.engine.bookmarks import InvalidError, NotFoundError
from pagewatch.engine.clock import iso
from pagewatch.engine.config import Resolved, resolve, resolve_candidate
from pagewatch.engine.fetch.base import FetchRequest
from pagewatch.engine.pipeline.autofilter import ProposeJob, propose_job
from pagewatch.engine.pipeline.core import (
    PreviewJob,
    RenderJob,
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
    return ViewVersion(row["raw_hash"], row["content_type"] or "", row["blocks_hash"])


VIEWS = ("highlight", "text", "new", "old", "screenshot")


async def render_change(
    engine: Engine, change_id: int, view: str, *, allow_remote: bool, context: int | None
) -> RenderOut:
    """One change's own gate diff (the history view): old version -> new version."""
    if view == "screenshot":
        raise NotFoundError("no screenshot was stored for this change")

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
    resolved = resolve(row, engine.folders, engine.settings)
    res = await engine.pool.run(
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
        ),
    )
    return RenderOut(html=res.html, view=res.view, identical=res.identical,
                     degraded=res.degraded, stats=res.stats)  # fmt: skip


async def render_unread(
    engine: Engine, bookmark_id: int, view: str, *, allow_remote: bool, context: int | None
) -> RenderOut:
    """The viewer's default: everything unread, baseline -> latest. The diff is computed in
    the worker pool and cached (one row per bookmark, replaced when either pointer moves)."""
    if view not in ("highlight", "text"):
        raise InvalidError("the unread diff supports view=highlight or view=text")

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
    if base["id"] != latest["id"]:
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
    res = await engine.pool.run(
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
        ),
    )
    return RenderOut(html=res.html, view=res.view, identical=res.identical,
                     degraded=res.degraded, stats=res.stats)  # fmt: skip


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
    res = await engine.pool.run(
        render_view,
        RenderJob(
            blob_root=str(engine.data_dir.blobs_dir),
            view=which,
            url=row["url"],
            new=_view_version(version) if which == "new" else None,
            old=_view_version(version) if which == "old" else None,
            allow_remote=allow_remote,
        ),
    )
    return RenderOut(html=res.html, view=res.view)


# -- add-bookmark preview -----------------------------------------------------------------


async def run_preview(engine: Engine, req: PreviewRequest) -> PreviewOut:
    """Trial fetch(es) of a URL with the given options. Nothing is stored."""
    try:
        fetch_cfg = FetchConfig.model_validate(req.fetch or {})
    except ValidationError as exc:
        raise InvalidError(str(exc.errors()[0]["msg"])) from exc
    resolved = Resolved(
        schedule=engine.settings.default_schedule,
        fetch=fetch_cfg,
        filter=FilterConfig(),
        gate=GateConfig(),
        actions=ActionsConfig(),
        overrides={},
    )
    samples: list[tuple[bytes, str]] = []
    first = None
    elapsed = 0
    for n in range(req.samples):
        if n:
            await engine.clock.sleep(req.gap_s)
        result = await engine.static_fetcher.fetch(
            FetchRequest(url=req.url, resolved=resolved, settings=engine.settings, force=True)
        )
        elapsed += result.elapsed_ms
        if result.error is not None:
            return PreviewOut(
                final_url=result.final_url, status=result.status, content_type="", kind="error",
                method="static", js_app=False, readable_chars=0, words=0, blocks=0, html="",
                error=f"{result.error.reason}: {result.error.message}", elapsed_ms=elapsed,
            )  # fmt: skip
        first = first or result
        samples.append((result.body, result.content_type))
    assert first is not None
    analysis = await engine.pool.run(
        preview_analyze,
        PreviewJob(
            samples, first.final_url, req.source_type.value, FilterConfig().model_dump(mode="json")
        ),
    )
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
        method="browser" if analysis.js_app else "static",
        js_app=analysis.js_app,
        readable_chars=analysis.chars,
        words=analysis.words,
        blocks=analysis.blocks,
        html=analysis.html,
        unstable_blocks=analysis.unstable_blocks,
        proposals=proposals,
        warnings=analysis.warnings,
        elapsed_ms=elapsed,
    )
