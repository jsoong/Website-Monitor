"""The filter assistant's engine side (spec: Desktop UI → Filter assistant, ``POST
/bookmarks/{id}/filter-assist``). Nothing is saved: every choice carries the ``PATCH`` body that
would add its rule, and the caller shows ``POST /bookmarks/{id}/test-filter`` for it first."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from pagewatch.engine.bookmarks import InvalidError, NotFoundError
from pagewatch.engine.changes import ConflictError
from pagewatch.engine.config import resolve
from pagewatch.engine.pipeline.assist import AssistError, AssistJob, assist
from pagewatch.engine.store import repo
from pagewatch.models import (
    FilterAssistElement,
    FilterAssistOut,
    FilterAssistRequest,
    FilterChoice,
    FilterRule,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine


async def run_assist(engine: Engine, bookmark_id: int, req: FilterAssistRequest) -> FilterAssistOut:
    def load(conn: sqlite3.Connection) -> tuple[Any, Any]:
        row = repo.bookmark_get(conn, bookmark_id)
        if row is None:
            return None, None
        wanted = row["latest_version_id"] if req.version == "new" else row["baseline_version_id"]
        if req.version_id is not None:
            wanted = req.version_id
        found = repo.version_get(conn, wanted)
        if found is not None and found["bookmark_id"] != bookmark_id:
            found = None  # someone else's version is not ours to read
        return row, found

    row, version = await engine.db.read(load)
    if row is None:
        raise NotFoundError(f"bookmark {bookmark_id} not found")
    if version is None or not version["raw_hash"]:
        raise ConflictError("this bookmark has no stored page yet; check it first")
    resolved = resolve(row, engine.folders, engine.settings)
    job = AssistJob(
        blob_root=str(engine.data_dir.blobs_dir),
        raw_hash=version["raw_hash"],
        content_type=version["content_type"] or "",
        blocks_hash=version["blocks_hash"],
        url=row["url"],
        source_type=row["source_type"],
        source_cfg=engine.source_cfg(row, resolved),
        ignore_case=resolved.filter.special.ignore_case,
        path=req.path,
        end_path=req.end_path,
        text=req.text,
        levels_up=req.levels_up,
    )
    try:
        result = await engine.pool.run(assist, job)
    except AssistError as exc:
        raise InvalidError(str(exc)) from exc

    # The lists a rule is appended to are the *effective* ones (folder defaults included): a
    # bookmark-level list replaces an inherited one, so appending to the stored list alone would
    # silently drop the folder's rules.
    existing = {
        "ignore": [
            r.model_dump(mode="json", exclude_defaults=True) for r in resolved.filter.ignore
        ],
        "watch": [r.model_dump(mode="json", exclude_defaults=True) for r in resolved.filter.watch],
    }
    choices: list[FilterChoice] = []
    for d in result.drafts:
        rule = FilterRule.model_validate(d.rule)
        dumped = rule.model_dump(mode="json", exclude_defaults=True)
        choices.append(
            FilterChoice(
                id=d.id,
                label=d.label,
                section=d.section,
                rule=rule,
                patch={"filter": {d.section: [*existing[d.section], dumped]}},
                explanation=d.explanation,
                warnings=d.warnings,
            )
        )
    return FilterAssistOut(
        element=FilterAssistElement.model_validate(result.element) if result.element else None,
        selection=result.selection,
        block_text=result.block_text,
        choices=choices,
    )
