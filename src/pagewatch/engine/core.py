"""``Engine``: the composition root. It owns every long-lived resource of one engine
process (database, blob store, worker pool, scheduler, fetchers, action queue, event bus,
settings) and their lifecycle.

It does not know about HTTP or the tray; ``api.server.ApiServer`` and ``main`` wrap it.
"""

from __future__ import annotations

import asyncio
import random
import secrets
import sqlite3
from collections.abc import Coroutine
from datetime import datetime, timedelta
from typing import Any, Literal

import psutil

from pagewatch import __version__
from pagewatch.engine import schedule as sched
from pagewatch.engine.actions.queue import ActionQueue
from pagewatch.engine.actions.toast import ToastBackend, ToastService, default_backend
from pagewatch.engine.api.events import EventBus
from pagewatch.engine.clock import Clock, SystemClock, iso, parse_iso
from pagewatch.engine.config import FolderCache, resolve
from pagewatch.engine.fetch.static import StaticFetcher
from pagewatch.engine.hostgate import HostGate
from pagewatch.engine.logs import get_logger, set_debug
from pagewatch.engine.paths import DataDir
from pagewatch.engine.pipeline.core import RebuildJob, rebuild_version
from pagewatch.engine.runner import CheckRunner
from pagewatch.engine.scheduler import Scheduler
from pagewatch.engine.settings import SettingsStore
from pagewatch.engine.store import repo
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.engine.store.db import Database, migrate
from pagewatch.engine.workers import WorkerPool
from pagewatch.models import AutowatchState, HealthOut, Settings, Trigger

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
        toast_backend: ToastBackend | None = None,
        rng: random.Random | None = None,
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
        self.folders = FolderCache()
        self.zone: sched.Zone = sched.make_zone(self.settings_store.current.timezone)
        self.online = True
        self.on_battery = False
        self.host_gate = HostGate(self.clock, lambda: self.settings)
        self.static_fetcher = StaticFetcher()
        self.runner = CheckRunner(self, rng)
        self.scheduler = Scheduler(
            self.clock,
            lambda: self.settings,
            self.host_gate,
            self.runner.run,
            on_autowatch_change=self._autowatch_changed,
        )
        self.actions = ActionQueue(self)
        self._toast_backend = toast_backend
        self.toasts: ToastService
        self._started_mono: float | None = None
        self.exit_code = EXIT_OK
        self._stopped = asyncio.Event()
        self._started = False
        self._background: set[asyncio.Task[Any]] = set()

    # -- properties ---------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self.settings_store.current

    def check_token(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate.encode(), self.token.encode())

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        """Run a fire-and-forget task, keeping a reference so it is not garbage collected."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain_background(self) -> None:
        """Wait for fire-and-forget work (re-normalising after a filter edit, ...)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self.data_dir.ensure()
        # Migrations run synchronously, before any thread touches the database, and take
        # an automatic backup first when upgrading an existing database.
        schema = migrate(self.data_dir.db_path, self.data_dir.backups_dir)
        self.db.start()
        await self.settings_store.load()
        self.pool = WorkerPool(self.settings.worker_processes, mode=self.pool.mode)
        self.zone = sched.make_zone(self.settings.timezone)
        set_debug(self.settings.debug_logging)
        if self._toast_backend is None:
            self._toast_backend = default_backend(asyncio.get_running_loop(), self.on_toast_action)
        self.toasts = ToastService(
            self.clock, self._toast_backend, lambda: self.settings.toast_coalesce_s
        )
        await self.reload_folders()
        # A crash can leave check_run rows open: close them as errors and re-queue.
        now = iso(self.clock.now())
        interrupted = await self.db.write(lambda c: repo.close_interrupted_runs(c, now))
        if interrupted:
            log.warning("interrupted_checks_requeued", bookmarks=interrupted)
        await self._load_scheduler()
        if self.settings.autowatch_state == "paused":
            until = (
                parse_iso(self.settings.autowatch_paused_until)
                if self.settings.autowatch_paused_until
                else None
            )
            self.scheduler.pause(until)
        self._started_mono = self.clock.monotonic()
        self._started = True
        self.scheduler.start()
        self.actions.start()
        log.info(
            "engine_started", version=self.version, schema=schema, data=str(self.data_dir.root)
        )

    async def _load_scheduler(self) -> None:
        rows = await self.db.read(repo.bookmark_schedule_rows)
        now = self.clock.now()
        loaded: list[tuple[int, str, int, str, bool, datetime | None]] = []
        for r in rows:
            due = parse_iso(r["next_due_at"]) if r["next_due_at"] else now
            loaded.append(
                (r["id"], r["url"], r["priority"], r["check_method"], bool(r["enabled"]), due)
            )
        self.scheduler.load(loaded)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        log.info("engine_stopping")
        await self.scheduler.stop(grace_s=10.0)
        await self.actions.stop()
        await self.toasts.aclose()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.static_fetcher.aclose()
        self.pool.shutdown()
        await asyncio.to_thread(self.db.close)
        self._stopped.set()

    def request_stop(self, exit_code: int = EXIT_OK) -> None:
        if self.exit_code == EXIT_OK:
            self.exit_code = exit_code
        self._stopped.set()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    # -- settings and folders -----------------------------------------------------------

    async def update_settings(self, patch: dict[str, Any]) -> Settings:
        new = await self.settings_store.update(patch)
        set_debug(new.debug_logging)
        self.zone = sched.make_zone(new.timezone)
        self.scheduler.wake()
        self.events.publish("engine_state", {"reason": "settings"})
        return new

    async def reload_folders(self) -> None:
        self.folders.load(await self.db.read(repo.folder_list))

    # -- bookmarks ----------------------------------------------------------------------

    def schedule_bookmark(self, row: sqlite3.Row) -> None:
        """(Re)register a bookmark row with the scheduler."""
        due = parse_iso(row["next_due_at"]) if row["next_due_at"] else None
        self.scheduler.upsert(
            row["id"],
            url=row["url"],
            priority=row["priority"],
            check_method=row["check_method"],
            enabled=bool(row["enabled"]),
            due=due,
        )

    async def mark_read(self, bookmark_id: int) -> bool:
        now = iso(self.clock.now())
        changed = await self.db.write(lambda c: repo.mark_read(c, bookmark_id, now))
        self.events.publish("bookmark_updated", {"bookmark_id": bookmark_id})
        return changed

    def check_now(self, ids: list[int], *, force: bool = False) -> int:
        return self.scheduler.check_now(ids, force=force)

    async def rebuild_versions(self, bookmark_id: int) -> None:
        """Re-run normalisation over the stored versions a bookmark's pointers reference,
        after its filter configuration changed, so that the next comparison is against
        like-for-like text (the raw-hash shortcut would otherwise hide the new filters)."""

        def load(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
            row = repo.bookmark_get(conn, bookmark_id)
            if row is None:
                return None, []
            ids = {
                row["latest_version_id"],
                row["baseline_version_id"],
                row["gate_anchor_version_id"],
            }
            versions = [v for i in ids if (v := repo.version_get(conn, i)) is not None]
            return row, versions

        row, versions = await self.db.read(load)
        if row is None:
            return
        resolved = resolve(row, self.folders, self.settings)
        updates: list[tuple[int, str, str, int]] = []
        for v in versions:
            if not v["raw_hash"]:
                continue
            job = RebuildJob(
                blob_root=str(self.data_dir.blobs_dir),
                raw_hash=v["raw_hash"],
                content_type=v["content_type"] or "",
                final_url=row["url"],
                source_type=row["source_type"],
                filter_cfg=resolved.filter.model_dump(mode="json"),
            )
            try:
                out = await self.pool.run(rebuild_version, job)
            except Exception:
                log.warning(
                    "rebuild_failed", bookmark_id=bookmark_id, version=v["id"], exc_info=True
                )
                continue
            updates.append((v["id"], out.blocks_hash, out.filtered_hash, out.word_count))

        def write(conn: sqlite3.Connection) -> None:
            for vid, blocks_hash, filtered_hash, words in updates:
                conn.execute(
                    "UPDATE version SET blocks_hash=?, filtered_hash=?, word_count=? WHERE id=?",
                    (blocks_hash, filtered_hash, words, vid),
                )
            conn.execute("DELETE FROM view_diff_cache WHERE bookmark_id=?", (bookmark_id,))

        await self.db.write(write)
        self.events.publish("bookmark_updated", {"bookmark_id": bookmark_id})

    # -- autowatch ----------------------------------------------------------------------

    async def set_autowatch(
        self, state: Literal["running", "paused"], until: datetime | None
    ) -> None:
        if state == "paused":
            self.scheduler.pause(until)
        else:
            self.scheduler.resume()
        await self.update_settings(
            {
                "autowatch_state": state,
                "autowatch_paused_until": iso(until) if (until and state == "paused") else None,
            }
        )

    def _autowatch_changed(self, paused: bool, until: datetime | None) -> None:
        """The scheduler resumed by itself (a timed pause expired): persist it."""
        self.spawn(
            self.update_settings(
                {
                    "autowatch_state": "paused" if paused else "running",
                    "autowatch_paused_until": None,
                }
            ),
            "persist-autowatch",
        )

    # -- notifications ------------------------------------------------------------------

    async def on_toast_action(self, action: str, bookmark_ids: list[int]) -> None:
        if action == "mark_read":
            for bid in bookmark_ids:
                await self.mark_read(bid)
        else:
            self.events.publish("open_change", {"bookmark_ids": bookmark_ids})

    def notify_problem(self, bookmark_id: int | None, title: str, body: str) -> None:
        """A bookmark-level problem (e.g. it started failing): one event and one toast."""
        self.events.publish(
            "problem",
            {"kind": "bookmark_error", "bookmark_id": bookmark_id, "message": f"{title}: {body}"},
        )
        self.spawn(self.toasts.notify(bookmark_id or 0, title, body), "problem-toast")

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
            return outcomes, repo.bookmark_count(conn)

        outcomes, bookmarks = await self.db.read(read)
        started = self._started_mono if self._started_mono is not None else self.clock.monotonic()
        until = self.scheduler.paused_until
        return HealthOut(
            version=self.version,
            uptime_s=max(0.0, self.clock.monotonic() - started),
            pid=psutil.Process().pid,
            queue_length=self.scheduler.queue_length,
            in_flight=self.scheduler.in_flight,
            outcomes_24h=outcomes,
            rss_mb=round(psutil.Process().memory_info().rss / 1_048_576, 1),
            autowatch=AutowatchState(
                state="paused" if self.scheduler.paused else "running",
                until=iso(until) if until else None,
            ),
            bookmarks=bookmarks,
            online=self.online,
            on_battery=self.on_battery,
            backlog_warning=self.scheduler.backlog_warning,
        )


__all__ = ["EXIT_ALREADY_RUNNING", "EXIT_ERROR", "EXIT_OK", "EXIT_RESTART", "Engine", "Trigger"]
