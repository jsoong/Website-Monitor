"""The Problems list (spec: *Desktop UI → Other screens*, *Actions → Execution rules*).

Two sources, both already persisted by earlier milestones, so this module only reads:

* bookmarks whose status is ``error`` or ``needs_login``, with the failure code of their last
  failing check (``check_run.reason``); and
* action jobs that used up their attempts (``action_job.status = 'failed'``), with the last error.

A problem clears by itself when its cause does: a bookmark leaves the list when a check succeeds or
the user marks it read (the M5 rule for the error counter), and a failed action leaves it when the
user reads the change it belongs to. Nothing here changes state.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from pagewatch.engine.store import repo
from pagewatch.models import ProblemOut, ProblemsOut

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


async def list_problems(engine: Engine, limit: int = DEFAULT_LIMIT) -> ProblemsOut:
    limit = max(1, min(limit, MAX_LIMIT))

    def read(conn: sqlite3.Connection) -> tuple[list[ProblemOut], int]:
        out: list[ProblemOut] = []
        for r in repo.problem_bookmarks(conn, limit):
            needs_login = r["status"] == "needs_login"
            out.append(
                ProblemOut(
                    kind="needs_login" if needs_login else "bookmark_error",
                    bookmark_id=r["id"],
                    bookmark_name=r["name"],
                    url=r["url"],
                    message=r["reason"] or ("needs login" if needs_login else "unknown error"),
                    occurred_at=r["at"],
                    consecutive_errors=r["consecutive_errors"],
                )
            )
        for r in repo.problem_actions(conn, limit):
            out.append(
                ProblemOut(
                    kind="action_failed",
                    bookmark_id=r["id"],
                    bookmark_name=r["name"],
                    url=r["url"],
                    message=r["last_error"] or "the action failed",
                    occurred_at=r["at"],
                    job_id=r["job_id"],
                    change_id=r["change_id"],
                    action_type=r["action_type"],
                    attempts=r["attempts"],
                )
            )
        return out, repo.problem_counts(conn)

    items, total = await engine.db.read(read)
    items.sort(key=lambda p: (p.occurred_at or "", p.bookmark_id), reverse=True)
    return ProblemsOut(items=items[:limit], total=total)
