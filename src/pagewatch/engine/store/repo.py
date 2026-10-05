"""All SQL lives here. Functions take an open ``sqlite3.Connection`` and never commit: the
caller (``Database.write`` / ``Database.read``) owns the transaction.

``commit_check`` is the one place where a check's results are applied: the version, the
change, the three pointers, the schedule, the action jobs and the check_run record all land
in a single transaction.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pagewatch.models import BookmarkStatus, Outcome

BOOKMARK_COLUMNS = (
    "folder_id", "name", "url", "source_type", "check_method", "enabled", "priority",
    "schedule_json", "fetch_json", "filter_json", "gate_json", "highlight_mode", "actions_json",
    "macro_id", "plugin", "info1", "info2", "info3", "note", "status", "unread",
    "consecutive_errors", "current_interval_s", "next_due_at", "last_checked_at",
    "last_changed_at", "latest_version_id", "baseline_version_id", "gate_anchor_version_id",
)  # fmt: skip


# -- folders ----------------------------------------------------------------------------


def folder_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM folder ORDER BY parent_id, sort_order, name").fetchall()


def folder_get(conn: sqlite3.Connection, folder_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM folder WHERE id=?", (folder_id,)
    ).fetchone()
    return row


def folder_insert(conn: sqlite3.Connection, f: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO folder(parent_id, name, sort_order, is_virtual, query_json, defaults_json) "
        "VALUES(?,?,?,?,?,?)",
        (
            f.get("parent_id"),
            f["name"],
            f.get("sort_order", 0),
            int(f.get("is_virtual", False)),
            f.get("query_json"),
            f.get("defaults_json", "{}"),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def folder_update(conn: sqlite3.Connection, folder_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE folder SET {cols} WHERE id=?", (*fields.values(), folder_id))


def folder_delete(conn: sqlite3.Connection, folder_id: int) -> None:
    conn.execute("DELETE FROM folder WHERE id=?", (folder_id,))


def folder_with_descendants(conn: sqlite3.Connection, folder_id: int) -> list[int]:
    rows = conn.execute(
        "WITH RECURSIVE t(id) AS (SELECT ? UNION ALL "
        "SELECT f.id FROM folder f JOIN t ON f.parent_id = t.id) SELECT id FROM t",
        (folder_id,),
    ).fetchall()
    return [r[0] for r in rows]


# -- bookmarks --------------------------------------------------------------------------


def bookmark_get(conn: sqlite3.Connection, bookmark_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM bookmark WHERE id=?", (bookmark_id,)
    ).fetchone()
    return row


def bookmark_insert(conn: sqlite3.Connection, fields: dict[str, Any], now: str) -> int:
    values = {k: v for k, v in fields.items() if k in BOOKMARK_COLUMNS}
    cols = [*values, "created_at", "updated_at"]
    cur = conn.execute(
        f"INSERT INTO bookmark({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
        (*values.values(), now, now),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def bookmark_update(
    conn: sqlite3.Connection, bookmark_id: int, fields: dict[str, Any], now: str
) -> None:
    values = {k: v for k, v in fields.items() if k in BOOKMARK_COLUMNS or k == "updated_at"}
    values["updated_at"] = now
    cols = ", ".join(f"{k}=?" for k in values)
    conn.execute(f"UPDATE bookmark SET {cols} WHERE id=?", (*values.values(), bookmark_id))


def bookmark_delete(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    n = 0
    for chunk in _chunks(list(ids), 500):
        marks = ",".join("?" * len(chunk))
        n += conn.execute(f"DELETE FROM bookmark WHERE id IN ({marks})", chunk).rowcount
    return n


def bookmark_schedule_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, url, enabled, priority, check_method, next_due_at, schedule_json, folder_id "
        "FROM bookmark"
    ).fetchall()


def bookmark_ids_in(conn: sqlite3.Connection, folder_ids: Sequence[int]) -> list[int]:
    marks = ",".join("?" * len(folder_ids))
    rows = conn.execute(
        f"SELECT id FROM bookmark WHERE folder_id IN ({marks}) ORDER BY id", list(folder_ids)
    ).fetchall()
    return [r[0] for r in rows]


def bookmark_all_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM bookmark WHERE enabled=1 ORDER BY id")]


def bookmark_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0])


SORTS = {
    "id": "id",
    "name": "name COLLATE NOCASE",
    "status": "status",
    "last_changed": "COALESCE(last_changed_at,'')",
    "last_checked": "COALESCE(last_checked_at,'')",
    "next_due": "COALESCE(next_due_at,'')",
    "errors": "consecutive_errors",
    "url": "url COLLATE NOCASE",
}


def _encode_cursor(sort: str, desc: bool, key: Any, row_id: int) -> str:
    raw = json.dumps({"s": sort, "d": desc, "k": key, "i": row_id})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(data, dict) or not {"s", "d", "k", "i"} <= data.keys():
            raise ValueError
        return data
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc


def bookmark_query(
    conn: sqlite3.Connection,
    *,
    folder_ids: Sequence[int] | None = None,
    status: str | None = None,
    unread: bool | None = None,
    enabled: bool | None = None,
    q: str | None = None,
    changed_since: str | None = None,
    keyword_hits: bool | None = None,
    sort: str = "id",
    desc: bool = False,
    cursor: str | None = None,
    limit: int = 100,
) -> tuple[list[sqlite3.Row], str | None, int]:
    """Keyset-paginated list. Returns ``(rows, next_cursor, total_matching)``."""
    if sort not in SORTS:
        raise ValueError(f"unknown sort {sort!r}")
    expr = SORTS[sort]
    where: list[str] = []
    args: list[Any] = []
    if folder_ids is not None:
        where.append(f"folder_id IN ({','.join('?' * len(folder_ids))})" if folder_ids else "0")
        args.extend(folder_ids)
    if status:
        where.append("status=?")
        args.append(status)
    if unread is not None:
        where.append("unread=?")
        args.append(int(unread))
    if enabled is not None:
        where.append("enabled=?")
        args.append(int(enabled))
    if changed_since:
        where.append("last_changed_at >= ?")
        args.append(changed_since)
    if keyword_hits:
        where.append(
            "EXISTS (SELECT 1 FROM change c WHERE c.bookmark_id = bookmark.id "
            "AND c.read_at IS NULL AND c.keyword_hits_json IS NOT NULL)"
        )
    if q:
        where.append("(name LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args.extend([like, like])
    base_where = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(conn.execute(f"SELECT COUNT(*) FROM bookmark{base_where}", args).fetchone()[0])

    page_where = list(where)
    page_args = list(args)
    if cursor:
        c = _decode_cursor(cursor)
        if c["s"] != sort or bool(c["d"]) != desc:
            raise ValueError("cursor does not match the requested sort")
        op = "<" if desc else ">"
        page_where.append(f"({expr}, id) {op} (?, ?)")
        page_args.extend([c["k"], c["i"]])
    order = "DESC" if desc else "ASC"
    sql = (
        f"SELECT *, {expr} AS _sortkey FROM bookmark"
        + ((" WHERE " + " AND ".join(page_where)) if page_where else "")
        + f" ORDER BY {expr} {order}, id {order} LIMIT ?"
    )
    rows = conn.execute(sql, [*page_args, limit + 1]).fetchall()
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        nxt = _encode_cursor(sort, desc, last["_sortkey"], last["id"])
    return rows, nxt, total


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# -- versions ---------------------------------------------------------------------------


def version_get(conn: sqlite3.Connection, version_id: int | None) -> sqlite3.Row | None:
    if version_id is None:
        return None
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM version WHERE id=?", (version_id,)
    ).fetchone()
    return row


def version_insert(conn: sqlite3.Connection, bookmark_id: int, fields: dict[str, Any]) -> int:
    cols = ["bookmark_id", *fields]
    cur = conn.execute(
        f"INSERT INTO version({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
        (bookmark_id, *fields.values()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def versions_since(conn: sqlite3.Connection, bookmark_id: int, version_id: int) -> int:
    """How many stored versions are newer than ``version_id`` (the checks a cumulative
    alert spans, including the newest)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM version WHERE bookmark_id=? AND id>?", (bookmark_id, version_id)
        ).fetchone()[0]
    )


# -- check runs -------------------------------------------------------------------------


def check_run_start(
    conn: sqlite3.Connection, bookmark_id: int, started_at: str, trigger: str, method: str
) -> int:
    cur = conn.execute(
        "INSERT INTO check_run(bookmark_id, started_at, trigger, method, outcome) "
        "VALUES(?,?,?,?, 'skipped')",
        (bookmark_id, started_at, trigger, method),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def check_runs_for(
    conn: sqlite3.Connection, bookmark_id: int, limit: int = 100, before_id: int | None = None
) -> list[sqlite3.Row]:
    if before_id is None:
        return conn.execute(
            "SELECT * FROM check_run WHERE bookmark_id=? ORDER BY id DESC LIMIT ?",
            (bookmark_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM check_run WHERE bookmark_id=? AND id<? ORDER BY id DESC LIMIT ?",
        (bookmark_id, before_id, limit),
    ).fetchall()


def close_interrupted_runs(conn: sqlite3.Connection, now: str) -> list[int]:
    """Runs left open by a crash become ``error: interrupted`` and their bookmarks are
    re-queued (due immediately). Returns the bookmark ids."""
    rows = conn.execute(
        "SELECT id, bookmark_id FROM check_run WHERE finished_at IS NULL"
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE check_run SET finished_at=?, outcome='error', reason='interrupted' WHERE id=?",
            (now, r["id"]),
        )
        conn.execute("UPDATE bookmark SET next_due_at=? WHERE id=?", (now, r["bookmark_id"]))
    return sorted({r["bookmark_id"] for r in rows})


# -- changes ----------------------------------------------------------------------------


def change_get(conn: sqlite3.Connection, change_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM change WHERE id=?", (change_id,)
    ).fetchone()
    return row


def changes_for(
    conn: sqlite3.Connection, bookmark_id: int, limit: int = 100, before_id: int | None = None
) -> list[sqlite3.Row]:
    if before_id is None:
        return conn.execute(
            "SELECT * FROM change WHERE bookmark_id=? ORDER BY id DESC LIMIT ?",
            (bookmark_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM change WHERE bookmark_id=? AND id<? ORDER BY id DESC LIMIT ?",
        (bookmark_id, before_id, limit),
    ).fetchall()


# -- action jobs ------------------------------------------------------------------------


def action_jobs_due(conn: sqlite3.Connection, now: str, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM action_job WHERE status='queued' AND (next_attempt_at IS NULL "
        "OR next_attempt_at<=?) ORDER BY id LIMIT ?",
        (now, limit),
    ).fetchall()


def action_job_next_attempt(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MIN(COALESCE(next_attempt_at, '')) FROM action_job WHERE status='queued'"
    ).fetchone()
    return None if row[0] is None else str(row[0])


# -- the check commit -------------------------------------------------------------------


@dataclass(slots=True)
class NewVersion:
    fetched_at: str
    raw_hash: str | None
    blocks_hash: str
    filtered_hash: str
    http_status: int | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    byte_size: int | None = None
    word_count: int | None = None
    screenshot_hash: str | None = None


@dataclass(slots=True)
class NewChange:
    old_version_id: int | None
    detected_at: str
    added_words: int | None
    removed_words: int | None
    changed_blocks: int | None
    anchor_based: bool
    keyword_hits: list[str] = field(default_factory=list)
    diff_hash: str | None = None
    summary: str | None = None


@dataclass(slots=True)
class CheckCommit:
    bookmark_id: int
    run_id: int
    finished_at: str
    outcome: Outcome
    reason: str | None
    duration_ms: int
    byte_count: int | None
    # bookmark updates
    next_due_at: str | None
    current_interval_s: int | None
    consecutive_errors: int | None = None  # None = leave alone
    status: BookmarkStatus | None = None
    # content results
    new_version: NewVersion | None = None
    is_first: bool = False
    change: NewChange | None = None
    action_types: list[str] = field(default_factory=list)  # one job each, by index
    refresh_etag_of: int | None = None  # latest version id whose etag/last-modified to refresh
    etag: str | None = None
    last_modified: str | None = None


@dataclass(slots=True)
class CommitResult:
    version_id: int | None = None
    change_id: int | None = None
    job_ids: list[int] = field(default_factory=list)


def commit_check(conn: sqlite3.Connection, c: CheckCommit) -> CommitResult:
    """Apply one check's results. Runs inside the writer thread's single transaction."""
    res = CommitResult()
    row = bookmark_get(conn, c.bookmark_id)
    if row is None:  # deleted while the check was running
        return res
    upd: dict[str, Any] = {
        "last_checked_at": c.finished_at,
        "next_due_at": c.next_due_at,
        "current_interval_s": c.current_interval_s,
    }
    if c.consecutive_errors is not None:
        upd["consecutive_errors"] = c.consecutive_errors
    status = c.status

    if c.new_version is not None:
        v = c.new_version
        res.version_id = version_insert(
            conn,
            c.bookmark_id,
            {
                "fetched_at": v.fetched_at,
                "raw_hash": v.raw_hash,
                "blocks_hash": v.blocks_hash,
                "filtered_hash": v.filtered_hash,
                "screenshot_hash": v.screenshot_hash,
                "http_status": v.http_status,
                "content_type": v.content_type,
                "etag": v.etag,
                "last_modified": v.last_modified,
                "byte_size": v.byte_size,
                "word_count": v.word_count,
            },
        )
        upd["latest_version_id"] = res.version_id
        conn.execute("DELETE FROM view_diff_cache WHERE bookmark_id=?", (c.bookmark_id,))
        if c.is_first:
            upd["baseline_version_id"] = res.version_id
            upd["gate_anchor_version_id"] = res.version_id
        if c.change is not None:
            ch = c.change
            accumulated = 1
            if ch.anchor_based and ch.old_version_id is not None:
                accumulated = max(1, versions_since(conn, c.bookmark_id, ch.old_version_id))
            cur = conn.execute(
                "INSERT INTO change(bookmark_id, old_version_id, new_version_id, detected_at, "
                "added_words, removed_words, changed_blocks, checks_accumulated, "
                "keyword_hits_json, diff_hash, summary) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    c.bookmark_id,
                    ch.old_version_id,
                    res.version_id,
                    ch.detected_at,
                    ch.added_words,
                    ch.removed_words,
                    ch.changed_blocks,
                    accumulated,
                    json.dumps(ch.keyword_hits) if ch.keyword_hits else None,
                    ch.diff_hash,
                    ch.summary,
                ),
            )
            res.change_id = cur.lastrowid
            upd["gate_anchor_version_id"] = res.version_id
            upd["unread"] = 1
            upd["last_changed_at"] = ch.detected_at
            status = BookmarkStatus.CHANGED
            for index, action_type in enumerate(c.action_types):
                job = conn.execute(
                    "INSERT INTO action_job(change_id, action_index, action_type, status) "
                    "VALUES(?,?,?, 'queued')",
                    (res.change_id, index, action_type),
                )
                assert job.lastrowid is not None
                res.job_ids.append(job.lastrowid)
    elif c.refresh_etag_of is not None and (c.etag or c.last_modified):
        conn.execute(
            "UPDATE version SET etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) "
            "WHERE id=?",
            (c.etag, c.last_modified, c.refresh_etag_of),
        )

    if status is None and row["status"] in ("new", "error") and c.outcome is not Outcome.ERROR:
        status = BookmarkStatus.CHANGED if row["unread"] else BookmarkStatus.OK
    if status is not None:
        upd["status"] = status.value
    if c.outcome is not Outcome.ERROR and c.consecutive_errors is None:
        upd["consecutive_errors"] = 0
    bookmark_update(conn, c.bookmark_id, upd, c.finished_at)
    conn.execute(
        "UPDATE check_run SET finished_at=?, outcome=?, reason=?, duration_ms=?, bytes=? "
        "WHERE id=?",
        (c.finished_at, c.outcome.value, c.reason, c.duration_ms, c.byte_count, c.run_id),
    )
    return res


# -- read state -------------------------------------------------------------------------


def mark_read(conn: sqlite3.Connection, bookmark_id: int, now: str) -> bool:
    """Promote latest to baseline and anchor, clear unread. Returns True if anything was unread."""
    row = bookmark_get(conn, bookmark_id)
    if row is None:
        return False
    latest = row["latest_version_id"]
    was_unread = bool(row["unread"]) or row["baseline_version_id"] != latest
    upd: dict[str, Any] = {
        "unread": 0,
        "baseline_version_id": latest,
        "gate_anchor_version_id": latest,
    }
    if row["status"] == "changed":
        upd["status"] = "ok"
    bookmark_update(conn, bookmark_id, upd, now)
    conn.execute(
        "UPDATE change SET read_at=? WHERE bookmark_id=? AND read_at IS NULL", (now, bookmark_id)
    )
    conn.execute("DELETE FROM view_diff_cache WHERE bookmark_id=?", (bookmark_id,))
    return was_unread


def unread_keyword_hits(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, list[str]]:
    """Keyword hits of each bookmark's unread changes (newest change first, de-duplicated)."""
    out: dict[int, list[str]] = {}
    for chunk in _chunks(list(ids), 500):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT bookmark_id, keyword_hits_json FROM change WHERE bookmark_id IN ({marks}) "
            "AND read_at IS NULL AND keyword_hits_json IS NOT NULL ORDER BY id DESC",
            chunk,
        ).fetchall()
        for r in rows:
            hits = out.setdefault(r["bookmark_id"], [])
            for h in json.loads(r["keyword_hits_json"]):
                if h not in hits:
                    hits.append(h)
    return out


def bookmark_counts(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    """Numbers for the folder tree: totals, built-in virtual folders, per-folder unread."""
    one = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(unread) AS unread, "
        "SUM(status='error') AS errors, "
        "SUM(status='needs_login') AS needs_login, "
        "SUM(last_changed_at >= ?) AS changed_today "
        "FROM bookmark",
        (since,),
    ).fetchone()
    hits = conn.execute(
        "SELECT COUNT(DISTINCT bookmark_id) FROM change "
        "WHERE read_at IS NULL AND keyword_hits_json IS NOT NULL"
    ).fetchone()[0]
    by_folder = {
        (r["folder_id"] if r["folder_id"] is not None else 0): {
            "total": r["n"],
            "unread": r["u"] or 0,
        }
        for r in conn.execute(
            "SELECT folder_id, COUNT(*) AS n, SUM(unread) AS u FROM bookmark GROUP BY folder_id"
        )
    }
    return {
        "total": one["total"],
        "unread": one["unread"] or 0,
        "errors": one["errors"] or 0,
        "needs_login": one["needs_login"] or 0,
        "changed_today": one["changed_today"] or 0,
        "keyword_hits": hits,
        "by_folder": by_folder,
    }
