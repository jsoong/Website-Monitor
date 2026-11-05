"""SQL for the Problems list of failed action jobs.

Kept beside ``repo.py`` rather than inside it (which holds the rest of the SQL) only so that this
milestone's change does not overlap the unattended-operation work in that file.
"""

from __future__ import annotations

import sqlite3

_SELECT = (
    "SELECT j.*, c.bookmark_id AS bookmark_id, c.detected_at AS detected_at, "
    "b.name AS bookmark_name FROM action_job j "
    "LEFT JOIN change c ON c.id = j.change_id "
    "LEFT JOIN bookmark b ON b.id = c.bookmark_id "
)


def action_jobs_list(conn: sqlite3.Connection, status: str | None, limit: int) -> list[sqlite3.Row]:
    """Newest first. ``status`` None means every status."""
    if status is None:
        return conn.execute(_SELECT + "ORDER BY j.id DESC LIMIT ?", (limit,)).fetchall()
    return conn.execute(
        _SELECT + "WHERE j.status = ? ORDER BY j.id DESC LIMIT ?", (status, limit)
    ).fetchall()


def action_job_view(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(_SELECT + "WHERE j.id = ?", (job_id,)).fetchone()
    return row


def action_job_retry(conn: sqlite3.Connection, job_id: int) -> bool:
    """Put a failed job back in the queue with a fresh set of attempts."""
    cur = conn.execute(
        "UPDATE action_job SET status='queued', attempts=0, next_attempt_at=NULL, last_error=NULL "
        "WHERE id=? AND status='failed'",
        (job_id,),
    )
    return cur.rowcount > 0


def action_job_dismiss(conn: sqlite3.Connection, job_id: int) -> bool:
    """Remove a failed job from the list (the user has seen it and does not want a retry)."""
    cur = conn.execute("DELETE FROM action_job WHERE id=? AND status='failed'", (job_id,))
    return cur.rowcount > 0
