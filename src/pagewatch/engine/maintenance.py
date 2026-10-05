"""The nightly chores (spec: Automatic backup; Data model -> Retention).

* A backup every day at ``backup_time`` local (03:00), or at the next wake if the machine was
  asleep or the engine was not running at that moment; the newest ``backup_keep`` automatic
  backups are kept.
* Retention and blob collection every day at ``maintenance_time`` (03:30), likewise.

"Due" means: the most recent occurrence of that clock time is later than the last successful run.
The last-run times are remembered in the database, so a restart neither repeats a job that ran
nor forgets one that did not. A job that fails is retried after an hour, not every minute.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Literal

from pagewatch.engine.clock import iso, parse_iso
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store.retention import Retention, RetentionReport

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.maintenance")

POLL_S = 60.0  # how often "is anything due?" is asked (and how soon a wake is noticed)
FIRST_DELAY_S = 120.0  # after start-up: the engine's own work comes first
RETRY_AFTER_S = 3600.0

Job = Literal["backup", "maintenance"]
STATE_KEY: dict[Job, str] = {"backup": "backup_last", "maintenance": "maintenance_last"}


class Maintenance:
    def __init__(self, engine: Engine, retention: Retention | None = None) -> None:
        self.e = engine
        self.retention = retention or Retention(
            engine.db, engine.blobs, engine.clock, lambda: engine.settings
        )
        self._task: asyncio.Task[None] | None = None
        self._retry_after: dict[Job, float] = {}
        self.last: dict[Job, datetime | None] = {"backup": None, "maintenance": None}
        self.last_retention: RetentionReport | None = None
        self.running: Job | None = None

    async def start(self) -> None:
        for job, key in STATE_KEY.items():
            text = await self.e.settings_store.get_state(key)
            self.last[job] = parse_iso(text) if text else None
        self._task = asyncio.create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # -- when ---------------------------------------------------------------------------

    def slot(self, now: datetime, clock_time: str) -> datetime:
        """The most recent occurrence of local ``clock_time`` at or before ``now`` (aware UTC)."""
        zone = self.e.zone
        local = zone.to_local(now)
        h, m = (int(x) for x in clock_time.split(":"))
        today = zone.localize(datetime.combine(local.date(), time(h, m)))
        if today <= now:
            return today
        return zone.localize(datetime.combine(local.date() - timedelta(days=1), time(h, m)))

    def due(self, job: Job, now: datetime | None = None) -> bool:
        now = now or self.e.clock.now()
        s = self.e.settings
        if job == "backup" and not s.backup_enabled:
            return False
        clock_time = s.backup_time if job == "backup" else s.maintenance_time
        last = self.last[job]
        if last is not None and last >= self.slot(now, clock_time):
            return False
        return self.e.clock.monotonic() >= self._retry_after.get(job, 0.0)

    # -- doing --------------------------------------------------------------------------

    async def _loop(self) -> None:
        await self.e.clock.sleep(FIRST_DELAY_S)
        while True:
            for job in ("backup", "maintenance"):
                if self.due(job):
                    await self.run(job)
            await self.e.clock.sleep(POLL_S)

    async def run(self, job: Job) -> bool:
        """Run one job now. Failure is logged and reported as a problem, never raised."""
        e = self.e
        self.running = job
        try:
            if job == "backup":
                info = await e.backup_now(kind="auto")
                detail = {"path": str(info.path), "bytes": info.size_bytes}
            else:
                self.last_retention = await self.retention.run()
                detail = self.last_retention.as_dict()
        except Exception as exc:
            log.exception("maintenance_failed", job=job)
            self._retry_after[job] = e.clock.monotonic() + RETRY_AFTER_S
            e.events.publish("problem", {"kind": f"{job}_failed", "message": str(exc)[:300]})
            return False
        finally:
            self.running = None
        now = e.clock.now()
        self.last[job] = now
        await e.settings_store.set_state(STATE_KEY[job], iso(now))
        self._retry_after.pop(job, None)
        e.events.publish(
            "engine_state",
            {
                "reason": job,
                **{k: v for k, v in detail.items() if isinstance(v, str | int | float)},
            },
        )
        return True
