"""The ``script`` action: WebSite-Watcher's script actions, as a declarative list of steps.

There is deliberately no scripting language (spec non-goal). The two operations the spec names:

``check_folder(name)``
    Check every bookmark in the folder called ``name`` (and its subfolders) now, ahead of the
    queue. A name that matches several folders checks all of them; one that matches none fails
    the job with a message saying so.
``pause_bookmark(minutes)``
    Do not check *this* bookmark again for ``minutes``: its next due time moves that far out
    (and is saved, so a restart keeps the pause). Its normal schedule continues afterwards.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING

from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import parse_params
from pagewatch.engine.clock import iso
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo
from pagewatch.models import ScriptParams

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.script")


def _folder_bookmarks(conn: sqlite3.Connection, name: str) -> list[int] | None:
    wanted = name.strip().casefold()
    roots = [
        r["id"]
        for r in repo.folder_list(conn)
        if r["name"].casefold() == wanted and not r["is_virtual"]
    ]
    if not roots:
        return None
    folder_ids: list[int] = []
    for root in roots:
        folder_ids += [f for f in repo.folder_with_descendants(conn, root) if f not in folder_ids]
    return repo.bookmark_ids_in(conn, folder_ids)


async def _check_folder(engine: Engine, name: str) -> None:
    ids = await engine.db.read(lambda c: _folder_bookmarks(c, name))
    if ids is None:
        raise ActionConfigError(f"script: there is no folder named {name!r}")
    queued = engine.check_now(ids)
    log.info("script_check_folder", folder=name, bookmarks=len(ids), queued=queued)


async def _pause(engine: Engine, bookmark_id: int, minutes: int) -> None:
    now = engine.clock.now()
    due = iso(now + timedelta(minutes=minutes))

    def write(conn: sqlite3.Connection) -> sqlite3.Row | None:
        repo.bookmark_update(conn, bookmark_id, {"next_due_at": due}, iso(now))
        return repo.bookmark_get(conn, bookmark_id)

    row = await engine.db.write(write)
    if row is None:
        return  # the bookmark was deleted while the job waited
    engine.schedule_bookmark(row)
    engine.events.publish("bookmark_updated", {"bookmark_id": bookmark_id})
    log.info("script_pause", bookmark=bookmark_id, minutes=minutes)


async def script(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(ScriptParams, ctx.params, "script")
    for step in params.steps:
        if step.op == "check_folder":
            assert step.name is not None
            await _check_folder(engine, step.name)
        else:
            assert step.minutes is not None
            await _pause(engine, ctx.bookmark_id, step.minutes)
