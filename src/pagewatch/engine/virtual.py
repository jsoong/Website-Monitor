"""Virtual folders: saved searches that behave like folders in the list.

A virtual folder stores a ``VirtualQuery`` in ``folder.query_json``. ``GET /bookmarks?folder=<id>``
resolves it here, so every client (UI, CLI, scripts) gets the same rows without knowing what a
saved search is. An explicit filter on the request (the search box, "unread only") narrows the
saved search further; two filters that cannot both hold (``status=error`` inside a search for
``status=changed``) match nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from pagewatch.engine.clock import iso
from pagewatch.engine.store import repo
from pagewatch.models import FolderCounts, VirtualQuery, loads

ALL_ROWS = 1_000_000


def load_query(conn: sqlite3.Connection, folder_id: int) -> VirtualQuery | None:
    """The saved search of ``folder_id``, or ``None`` if it is missing or a real folder."""
    row = repo.folder_get(conn, folder_id)
    if row is None or not row["is_virtual"]:
        return None
    try:
        return VirtualQuery.model_validate(loads(row["query_json"], {}) or {})
    except ValidationError:
        return VirtualQuery(min_errors=10**9)  # a corrupt query matches nothing


def filters_of(conn: sqlite3.Connection, vq: VirtualQuery, now: datetime) -> dict[str, Any]:
    """``bookmark_query`` keyword arguments for a saved search."""
    out: dict[str, Any] = {}
    if vq.q:
        out["extra_q"] = vq.q
    if vq.status is not None:
        out["status"] = vq.status.value
    if vq.unread is not None:
        out["unread"] = vq.unread
    if vq.enabled is not None:
        out["enabled"] = vq.enabled
    if vq.folder is not None:
        out["folder_ids"] = repo.folder_with_descendants(conn, vq.folder)
    if vq.changed_within_h is not None:
        out["changed_since"] = iso(now - timedelta(hours=vq.changed_within_h))
    if vq.keyword_hits is not None:
        out["keyword_hits"] = vq.keyword_hits or None
    if vq.source_type is not None:
        out["source_type"] = vq.source_type.value
    if vq.check_method is not None:
        out["check_method"] = vq.check_method.value
    if vq.plugin is not None:
        out["plugin"] = vq.plugin
    if vq.min_errors is not None:
        out["min_errors"] = vq.min_errors
    return out


def narrow(saved: dict[str, Any], explicit: dict[str, Any]) -> dict[str, Any] | None:
    """Both filter sets must hold. ``None`` means they contradict each other (no rows)."""
    merged = dict(saved)
    for key, value in explicit.items():
        if value is None:
            continue
        if key == "q":
            merged["q"] = value  # the saved search's own text is ``extra_q``: both apply
        elif key == "folder_ids":
            have = merged.get("folder_ids")
            if have is not None:
                value = [f for f in value if f in set(have)]
            merged["folder_ids"] = value
        elif key in merged and merged[key] not in (None, value):
            if key == "changed_since":
                merged[key] = max(merged[key], value)  # the later start satisfies both
            else:
                return None
        else:
            merged[key] = value
    return merged


def query_rows(
    conn: sqlite3.Connection, vq: VirtualQuery, now: datetime, **explicit: Any
) -> tuple[list[sqlite3.Row], str | None, int]:
    """``repo.bookmark_query`` through a saved search plus the request's own filters."""
    paging = {k: explicit.pop(k) for k in ("sort", "desc", "cursor", "limit") if k in explicit}
    merged = narrow(filters_of(conn, vq, now), explicit)
    if merged is None:
        return [], None, 0
    return repo.bookmark_query(conn, **merged, **paging)


def bookmark_ids(conn: sqlite3.Connection, folder_id: int, now: datetime) -> list[int]:
    """Every bookmark a saved search matches (for ``POST /check``)."""
    vq = load_query(conn, folder_id)
    if vq is None:
        return []
    rows, _, _ = query_rows(conn, vq, now, limit=ALL_ROWS)
    return [r["id"] for r in rows]


def counts(conn: sqlite3.Connection, now: datetime) -> dict[int, FolderCounts]:
    """Totals and unread counts of every saved search, for the folder tree."""
    out: dict[int, FolderCounts] = {}
    for f in repo.folder_list(conn):
        if not f["is_virtual"]:
            continue
        vq = load_query(conn, f["id"])
        assert vq is not None
        _, _, total = query_rows(conn, vq, now, limit=1)
        _, _, unread = query_rows(conn, vq, now, limit=1, unread=True)
        out[f["id"]] = FolderCounts(total=total, unread=unread)
    return out
