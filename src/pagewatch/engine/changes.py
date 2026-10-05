"""Operations on changes and on candidate filter configurations (nothing here stores a filter:
the user confirms by patching the bookmark)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from pagewatch.engine.bookmarks import InvalidError, NotFoundError
from pagewatch.engine.config import resolve, resolve_candidate
from pagewatch.engine.pipeline.autofilter import ProposeJob, propose_job
from pagewatch.engine.pipeline.core import TestFilterJob, test_filter
from pagewatch.engine.store import repo
from pagewatch.models import (
    FalsePositiveOut,
    ProposalOut,
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
