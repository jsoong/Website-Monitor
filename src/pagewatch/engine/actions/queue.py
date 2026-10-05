"""Durable action queue.

Each action of a change is an ``action_job`` row keyed by ``(change, action_index)``, so
reruns are idempotent. Jobs run in configured order; a failure retries after 1, 5 and 30
minutes (then every 30) up to 5 attempts, after which the job is ``failed`` and surfaces in
the Problems list. Jobs survive restarts because they live in SQLite: a job interrupted by a
crash is simply run again (at-least-once delivery).

The jobs of one change run one at a time, in configured order (``mark_read`` is always last):
a job waits while an earlier job of its change is running or waiting for a retry. If an earlier
action finally failed, ``mark_read`` does not run, so a change the user was never told about
stays unread.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal

from pagewatch.engine.actions.base import ActionUnavailable, AlertContext
from pagewatch.engine.actions.builtin import ACTIONS
from pagewatch.engine.clock import iso, parse_iso
from pagewatch.engine.config import resolve
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store import repo
from pagewatch.models import AlertPrivacy, loads

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.actions")

BACKOFF_MINUTES = (1, 5, 30)
MAX_ATTEMPTS = 5
MAX_IDLE_WAIT_S = 60.0
CONCURRENCY = 8


def backoff_for(attempts_done: int) -> timedelta:
    idx = min(attempts_done, len(BACKOFF_MINUTES)) - 1
    return timedelta(minutes=BACKOFF_MINUTES[max(0, idx)])


class ActionQueue:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: dict[int, asyncio.Task[None]] = {}
        self._sem = asyncio.Semaphore(CONCURRENCY)
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="action-queue")

    def kick(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None
        tasks = list(self._running.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self, max_wait_s: float = 30.0) -> None:
        """Test helper: until no job is running or due."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        while True:
            now = iso(self.e.clock.now())
            due = await self.e.db.read(partial(repo.action_jobs_due, now=now, limit=1))
            if not due and not self._running:
                return
            if loop.time() > deadline:
                raise TimeoutError("action queue not idle")
            await asyncio.sleep(0.005)

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            now = iso(self.e.clock.now())
            jobs = await self.e.db.read(partial(repo.action_jobs_due, now=now, limit=100))
            for job in jobs:
                if job["id"] not in self._running:
                    self._running[job["id"]] = asyncio.create_task(
                        self._run_job(job["id"]), name=f"action-{job['id']}"
                    )
            sleeper = asyncio.ensure_future(self.e.clock.sleep(await self._sleep_for()))
            waker = asyncio.ensure_future(self._wake.wait())
            _, pending = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _sleep_for(self) -> float:
        """Until the next scheduled retry, but never longer than a minute (clock changes)."""
        nxt = await self.e.db.read(repo.action_job_next_attempt)
        if nxt is None:
            return MAX_IDLE_WAIT_S
        wait = (parse_iso(nxt) - self.e.clock.now()).total_seconds()
        return min(MAX_IDLE_WAIT_S, max(0.05, wait))

    # -- one job ------------------------------------------------------------------------

    def _context(
        self, conn: sqlite3.Connection, job_id: int
    ) -> tuple[sqlite3.Row, AlertContext] | Literal["stale", "orphan"]:
        """Load a job to run. ``stale``: it is gone or no longer queued (the loop's snapshot
        was out of date: running it again would send a duplicate). ``orphan``: its change or
        bookmark was deleted."""
        job = conn.execute("SELECT * FROM action_job WHERE id=?", (job_id,)).fetchone()
        if job is None or job["status"] != "queued":
            return "stale"
        change = repo.change_get(conn, job["change_id"])
        row = repo.bookmark_get(conn, change["bookmark_id"]) if change else None
        if change is None or row is None:
            return "orphan"
        resolved = resolve(row, self.e.folders, self.e.settings)
        actions = resolved.actions.actions
        idx = job["action_index"]
        params = actions[idx].params if idx < len(actions) else {}
        ctx = AlertContext(
            bookmark_id=row["id"],
            name=row["name"],
            url=row["url"],
            info=(row["info1"], row["info2"], row["info3"]),
            change_id=change["id"],
            detected_at=change["detected_at"],
            summary=change["summary"],
            added_words=change["added_words"],
            removed_words=change["removed_words"],
            changed_blocks=change["changed_blocks"],
            checks_accumulated=change["checks_accumulated"],
            keyword_hits=loads(change["keyword_hits_json"], []) or [],
            private=resolved.actions.alert_privacy is AlertPrivacy.PRIVATE,
            params=dict(params),
        )
        return job, ctx

    async def _run_job(self, job_id: int) -> None:
        try:
            async with self._sem:
                loaded = await self.e.db.read(lambda c: self._context(c, job_id))
                if loaded == "stale":
                    return
                if loaded == "orphan":
                    await self.e.db.write(
                        lambda c: c.execute("DELETE FROM action_job WHERE id=?", (job_id,))
                    )
                    return
                job, ctx = loaded
                if job["action_type"] == "mark_read" and await self.e.db.read(
                    lambda c: repo.action_job_earlier_failed(c, job)
                ):
                    await self.e.db.write(
                        lambda c: c.execute(
                            "UPDATE action_job SET status='done', last_error=?, "
                            "next_attempt_at=NULL WHERE id=?",
                            ("skipped: an earlier action failed, so the change stays unread",
                             job_id),
                        )
                    )  # fmt: skip
                    return
                action = ACTIONS.get(job["action_type"])
                try:
                    if action is None:
                        raise ActionUnavailable(f"unknown action {job['action_type']!r}")
                    await action(self.e, ctx)
                except asyncio.CancelledError:
                    raise
                except ActionUnavailable as exc:
                    await self._fail(
                        job_id, ctx, str(exc), final=True, attempts=job["attempts"] + 1
                    )
                except Exception as exc:
                    log.warning(
                        "action_failed", job=job_id, type=job["action_type"], error=str(exc)
                    )
                    attempts = job["attempts"] + 1
                    await self._fail(
                        job_id, ctx, f"{type(exc).__name__}: {exc}"[:500],
                        final=attempts >= MAX_ATTEMPTS, attempts=attempts,
                    )  # fmt: skip
                else:
                    await self.e.db.write(
                        lambda c: c.execute(
                            "UPDATE action_job SET status='done', attempts=attempts+1, "
                            "last_error=NULL, next_attempt_at=NULL WHERE id=?",
                            (job_id,),
                        )
                    )
        finally:
            self._running.pop(job_id, None)
            self._wake.set()

    async def _fail(
        self, job_id: int, ctx: AlertContext, error: str, *, final: bool, attempts: int
    ) -> None:
        now = self.e.clock.now()
        nxt = None if final else iso(now + backoff_for(attempts))
        status = "failed" if final else "queued"

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE action_job SET status=?, attempts=?, last_error=?, next_attempt_at=? "
                "WHERE id=?",
                (status, attempts, error, nxt, job_id),
            )

        await self.e.db.write(write)
        if final:
            self.e.events.publish(
                "problem",
                {
                    "kind": "action_failed",
                    "bookmark_id": ctx.bookmark_id,
                    "job_id": job_id,
                    "error": error,
                },
            )
