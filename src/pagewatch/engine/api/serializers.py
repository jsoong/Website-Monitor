"""Database rows -> API models."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from pagewatch.engine.config import effective_section, resolve
from pagewatch.models import (
    BookmarkOut,
    BookmarkSummary,
    ChangeOut,
    CheckRunOut,
    FolderOut,
    ScheduleMode,
    loads,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine


def folder_out(row: sqlite3.Row) -> FolderOut:
    return FolderOut(
        id=row["id"],
        parent_id=row["parent_id"],
        name=row["name"],
        sort_order=row["sort_order"],
        is_virtual=bool(row["is_virtual"]),
        query=loads(row["query_json"], None),
        defaults=loads(row["defaults_json"], {}) or {},
    )


def bookmark_out(engine: Engine, row: sqlite3.Row) -> BookmarkOut:
    r = resolve(row, engine.folders, engine.settings)
    return BookmarkOut(
        id=row["id"],
        folder_id=row["folder_id"],
        name=row["name"],
        url=row["url"],
        source_type=row["source_type"],
        check_method=row["check_method"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        schedule=r.schedule,
        fetch=r.fetch,
        filter=r.filter,
        gate=r.gate,
        highlight_mode=row["highlight_mode"],
        actions=r.actions,
        overrides=r.overrides,
        macro_id=row["macro_id"],
        plugin=row["plugin"],
        info1=row["info1"],
        info2=row["info2"],
        info3=row["info3"],
        note=row["note"],
        status=row["status"],
        unread=bool(row["unread"]),
        consecutive_errors=row["consecutive_errors"],
        current_interval_s=row["current_interval_s"],
        next_due_at=row["next_due_at"],
        last_checked_at=row["last_checked_at"],
        last_changed_at=row["last_changed_at"],
        latest_version_id=row["latest_version_id"],
        baseline_version_id=row["baseline_version_id"],
        gate_anchor_version_id=row["gate_anchor_version_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def bookmark_summary(engine: Engine, row: sqlite3.Row) -> BookmarkSummary:
    sched = effective_section("schedule", row, engine.folders, engine.settings)
    mode = ScheduleMode(sched.get("mode", "interval"))
    interval = (
        row["current_interval_s"] if mode is ScheduleMode.ADAPTIVE else sched.get("interval_s")
    )
    return BookmarkSummary(
        id=row["id"],
        folder_id=row["folder_id"],
        name=row["name"],
        url=row["url"],
        source_type=row["source_type"],
        check_method=row["check_method"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        status=row["status"],
        unread=bool(row["unread"]),
        consecutive_errors=row["consecutive_errors"],
        interval_s=interval,
        schedule_mode=mode,
        next_due_at=row["next_due_at"],
        last_checked_at=row["last_checked_at"],
        last_changed_at=row["last_changed_at"],
    )


def change_out(row: sqlite3.Row) -> ChangeOut:
    hits = loads(row["keyword_hits_json"], []) or []
    return ChangeOut(
        id=row["id"],
        bookmark_id=row["bookmark_id"],
        old_version_id=row["old_version_id"],
        new_version_id=row["new_version_id"],
        detected_at=row["detected_at"],
        added_words=row["added_words"],
        removed_words=row["removed_words"],
        changed_blocks=row["changed_blocks"],
        checks_accumulated=row["checks_accumulated"],
        keyword_hits=hits if isinstance(hits, list) else json.loads(hits),
        summary=row["summary"],
        feedback=row["feedback"],
        read_at=row["read_at"],
    )


def check_run_out(row: sqlite3.Row) -> CheckRunOut:
    return CheckRunOut(
        id=row["id"],
        bookmark_id=row["bookmark_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        trigger=row["trigger"],
        method=row["method"],
        outcome=row["outcome"],
        reason=row["reason"],
        duration_ms=row["duration_ms"],
        bytes=row["bytes"],
    )
