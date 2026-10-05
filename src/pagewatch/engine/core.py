"""``Engine``: the composition root. It owns every long-lived resource of one engine
process (database, blob store, worker pool, event bus, settings) and their lifecycle.

It does not know about HTTP or the tray; ``api.server.ApiServer`` and ``main`` wrap it.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
from datetime import timedelta
from typing import Any, Literal

import psutil

from pagewatch import __version__
from pagewatch.engine.api.events import EventBus
from pagewatch.engine.clock import Clock, SystemClock, iso
from pagewatch.engine.logs import get_logger, set_debug
from pagewatch.engine.paths import DataDir
from pagewatch.engine.settings import SettingsStore
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.engine.store.db import Database, migrate
from pagewatch.engine.workers import WorkerPool
from pagewatch.models import AutowatchState, HealthOut, Settings

log = get_logger("pagewatch.engine")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALREADY_RUNNING = 2
EXIT_RESTART = 3


class Engine:
    def __init__(
        self,
        data_dir: DataDir,
        *,
        clock: Clock | None = None,
        worker_mode: Literal["process", "thread"] = "process",
        settings_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.clock: Clock = clock or SystemClock()
        self.version = __version__
        self.token = secrets.token_urlsafe(32)
        self.events = EventBus(self.clock)
        self.db = Database(data_dir.db_path)
        self.blobs = BlobStore(data_dir.blobs_dir)
        self.settings_store = SettingsStore(self.db, settings_overrides)
        self.pool = WorkerPool(self.settings_store.current.worker_processes, mode=worker_mode)
        self._started_mono: float | None = None
        self.exit_code = EXIT_OK
        self._stopped = asyncio.Event()
        self._started = False

    # -- properties ---------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self.settings_store.current

    def check_token(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate.encode(), self.token.encode())

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self.data_dir.ensure()
        # Migrations run synchronously, before any thread touches the database, and take
        # an automatic backup first when upgrading an existing database.
        version = migrate(self.data_dir.db_path, self.data_dir.backups_dir)
        self.db.start()
        await self.settings_store.load()
        self.pool = WorkerPool(self.settings.worker_processes, mode=self.pool.mode)
        set_debug(self.settings.debug_logging)
        self._started_mono = self.clock.monotonic()
        self._started = True
        log.info(
            "engine_started", version=self.version, schema=version, data=str(self.data_dir.root)
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        log.info("engine_stopping")
        self.pool.shutdown()
        await asyncio.to_thread(self.db.close)
        self._stopped.set()

    def request_stop(self, exit_code: int = EXIT_OK) -> None:
        if self.exit_code == EXIT_OK:
            self.exit_code = exit_code
        self._stopped.set()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    # -- settings -----------------------------------------------------------------------

    async def update_settings(self, patch: dict[str, Any]) -> Settings:
        new = await self.settings_store.update(patch)
        set_debug(new.debug_logging)
        self.events.publish("engine_state", {"reason": "settings"})
        return new

    # -- health -------------------------------------------------------------------------

    async def health(self) -> HealthOut:
        since = iso(self.clock.now() - timedelta(hours=24))

        def read(conn: sqlite3.Connection) -> tuple[dict[str, int], int]:
            outcomes = {
                r["outcome"]: r["n"]
                for r in conn.execute(
                    "SELECT outcome, COUNT(*) AS n FROM check_run "
                    "WHERE started_at >= ? AND finished_at IS NOT NULL GROUP BY outcome",
                    (since,),
                )
            }
            count = int(conn.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0])
            return outcomes, count

        outcomes, bookmarks = await self.db.read(read)
        started = self._started_mono if self._started_mono is not None else self.clock.monotonic()
        return HealthOut(
            version=self.version,
            uptime_s=max(0.0, self.clock.monotonic() - started),
            pid=psutil.Process().pid,
            queue_length=0,
            in_flight=0,
            outcomes_24h=outcomes,
            rss_mb=round(psutil.Process().memory_info().rss / 1_048_576, 1),
            autowatch=AutowatchState(state=self.settings.autowatch_state),
            bookmarks=bookmarks,
        )
