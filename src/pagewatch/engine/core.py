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
from pathlib import Path
from typing import Any, Literal

import psutil

from pagewatch import __version__
from pagewatch.engine import schedule as sched
from pagewatch.engine.actions.queue import ActionQueue
from pagewatch.engine.actions.toast import ToastBackend, ToastService, default_backend
from pagewatch.engine.api.events import EventBus
from pagewatch.engine.clock import Clock, SystemClock, iso, parse_iso
from pagewatch.engine.config import FolderCache, resolve, resolve_schedule, source_options
from pagewatch.engine.fetch.browser import BrowserFetcher, BrowserManager, Launcher
from pagewatch.engine.fetch.feed import FeedFetcher
from pagewatch.engine.fetch.ftp import FtpFetcher
from pagewatch.engine.fetch.localfile import FileFetcher
from pagewatch.engine.fetch.screenshot import ScreenshotFetcher
from pagewatch.engine.fetch.select import FetcherSet
from pagewatch.engine.fetch.static import StaticFetcher
from pagewatch.engine.guard import ResourceGuard, tree_rss_mb
from pagewatch.engine.hostgate import HostGate
from pagewatch.engine.logs import get_logger, set_debug
from pagewatch.engine.maintenance import Maintenance
from pagewatch.engine.paths import DataDir
from pagewatch.engine.pipeline.core import RebuildJob, rebuild_version
from pagewatch.engine.power import Connectivity, PowerBackend, PowerMonitor, ProbeFn
from pagewatch.engine.runner import CheckRunner
from pagewatch.engine.scheduler import Scheduler
from pagewatch.engine.secrets import NullSecrets, SecretStore
from pagewatch.engine.settings import SettingsStore
from pagewatch.engine.store import backup as backups
from pagewatch.engine.store import repo
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.engine.store.db import Database, migrate
from pagewatch.engine.tray import TrayBackend, TrayController
from pagewatch.engine.workers import WorkerPool
from pagewatch.models import AutowatchState, HealthOut, OnBattery, Settings, Trigger

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
        tray_backend: TrayBackend | None = None,
        enable_tray: bool = False,
        browser_launcher: Launcher | None = None,
        secret_store: SecretStore | None = None,
        enable_unattended: bool = False,
        power_backend: PowerBackend | None = None,
        probe: ProbeFn | None = None,
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
        self.secrets: SecretStore = secret_store or NullSecrets()
        self.static_fetcher = StaticFetcher()
        self.browser = BrowserManager(lambda: self.settings, self.clock, browser_launcher)
        self.fetchers = FetcherSet(
            {
                "static": self.static_fetcher,
                "feed": FeedFetcher(self.static_fetcher),
                "browser": BrowserFetcher(self.browser),
                "screenshot": ScreenshotFetcher(self.browser),
                "ftp": FtpFetcher(self.secrets),
                "file": FileFetcher(),
            }
        )
        self.runner = CheckRunner(self, rng)
        self.scheduler = Scheduler(
            self.clock,
            lambda: self.settings,
            self.host_gate,
            self.runner.run,
            on_autowatch_change=self._autowatch_changed,
        )
        self.actions = ActionQueue(self)
        self.tray: TrayController | None = (
            TrayController(self, tray_backend) if (enable_tray or tray_backend) else None
        )
        # Unattended-operation machinery (M5): off unless asked for, like the tray, so unit tests
        # never probe a real network or start background loops they did not ask for.
        self.connectivity: Connectivity | None = None
        self.power: PowerMonitor | None = None
        self.maintenance: Maintenance | None = None
        self.guard: ResourceGuard | None = None
        if enable_unattended:
            self.connectivity = Connectivity(self, probe)
            self.power = PowerMonitor(self, power_backend)
            self.maintenance = Maintenance(self)
            self.guard = ResourceGuard(self)
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
        # A restore staged by the previous run is applied before anything opens the database.
        if backups.apply_pending_restore(self.data_dir, self.clock.now()):
            log.warning("restore_applied_at_start")
        # Migrations run synchronously, before any thread touches the database, and take
        # an automatic backup first when upgrading an existing database.
        schema = migrate(self.data_dir.db_path, self.data_dir.backups_dir)
        self.db.start()
        await self.settings_store.load()
        self.pool = WorkerPool(self.settings.worker_processes, mode=self.pool.mode)
        self.pool.default_timeout_s = self.settings.worker_job_timeout_s
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
        # Nothing is dispatched until the start-up sequence below has looked at the network and
        # staggered whatever is overdue (a restart after hours would otherwise run everything
        # at once).
        self.scheduler.hold("startup")
        self.scheduler.start()
        self.actions.start()
        if self.tray is not None:
            await self.tray.start()
        try:
            if self.power is not None:
                await self.power.start()
            if self.connectivity is not None and not await self.connectivity.probe(fresh=True):
                self.connectivity.go_offline("startup")  # keeps probing; catches up when it answers
            elif self.connectivity is not None:
                await self.catch_up("startup")
        finally:
            self.scheduler.release("startup")
        if self.maintenance is not None:
            await self.maintenance.start()
        if self.guard is not None:
            await self.guard.start()
        log.info(
            "engine_started", version=self.version, schema=schema, data=str(self.data_dir.root)
        )

    async def _load_scheduler(self) -> None:
        rows = await self.db.read(repo.bookmark_schedule_rows)
        now = self.clock.now()
        loaded: list[tuple[int, str, int, str, bool, datetime | None, bool]] = []
        for r in rows:
            due = parse_iso(r["next_due_at"]) if r["next_due_at"] else now
            loaded.append(
                (r["id"], r["url"], r["priority"], r["check_method"], bool(r["enabled"]), due,
                 self._pauses_on_battery(r))
            )  # fmt: skip
        self.scheduler.load(loaded)

    def _pauses_on_battery(self, row: sqlite3.Row) -> bool:
        cfg = resolve_schedule(row, self.folders, self.settings)
        return cfg is not None and cfg.on_battery is OnBattery.PAUSE

    async def refresh_battery_policies(self) -> None:
        """A folder's defaults changed: bookmarks that inherit them may have a new policy."""
        for r in await self.db.read(repo.bookmark_schedule_rows):
            self.scheduler.set_battery_pause(r["id"], self._pauses_on_battery(r))

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        log.info("engine_stopping")
        if self.tray is not None:
            await self.tray.stop()
        if self.guard is not None:
            await self.guard.stop()
        if self.maintenance is not None:
            await self.maintenance.stop()
        if self.power is not None:
            await self.power.stop()
        if self.connectivity is not None:
            await self.connectivity.stop()
        await self.scheduler.stop(grace_s=10.0)
        await self.actions.stop()
        await self.toasts.aclose()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.fetchers.aclose()
        await self.static_fetcher.aclose()
        await self.browser.close()
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
        self.pool.default_timeout_s = new.worker_job_timeout_s
        if self.power is not None:
            self.power.apply_keep_awake()
            self.power.poll_battery()  # the battery-saver option may have just been turned on
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
            battery_pause=self._pauses_on_battery(row),
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
                source_cfg=source_options(resolved.fetch),
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

    # -- unattended operation -----------------------------------------------------------

    def set_online(self, online: bool, reason: str = "", *, catch_up: bool = False) -> None:
        """Offline mode: scheduled dispatch stops and failures are not counted until the
        network answers again. With ``catch_up`` the overdue bookmarks are re-timed (staggered)
        *before* dispatch resumes, so they do not all start at once."""
        if online == self.online:
            return
        self.online = online
        hold = f"catchup:{reason or 'online'}" if (online and catch_up) else None
        if hold:
            self.scheduler.hold(hold)
        self.scheduler.set_online(online)
        log.info("online" if online else "offline", reason=reason)
        self.events.publish(
            "engine_state", {"reason": "online" if online else "offline", "detail": reason}
        )
        if hold:
            self.spawn(self._catch_up_released("network", hold), "catch-up-network")

    def set_on_battery(self, on_battery: bool) -> None:
        if on_battery == self.on_battery:
            return
        self.on_battery = on_battery
        hold = "catchup:ac" if (not on_battery and self._started) else None
        if hold:  # what the policy held back is re-timed before it is let go
            self.scheduler.hold(hold)
        self.scheduler.set_on_battery(on_battery)
        log.info("power_source", on_battery=on_battery)
        self.events.publish("engine_state", {"reason": "battery", "on_battery": on_battery})
        if hold:
            self.spawn(self._catch_up_released("ac", hold), "catch-up-ac")

    async def _catch_up_released(self, reason: str, hold: str) -> None:
        try:
            await self.catch_up(reason)
        finally:
            self.scheduler.release(hold)

    async def catch_up(self, reason: str) -> int:
        """One ``catchup`` check for every overdue bookmark, staggered (spec: Catch up once). A
        bookmark whose ``days`` / ``window`` forbid running right now is moved to its next
        allowed start instead of being checked at 3 a.m."""
        overdue = self.scheduler.overdue_ids()
        if not overdue:
            return 0
        wanted = set(overdue)
        now = self.clock.now()
        moved: dict[int, datetime] = {}
        for r in await self.db.read(repo.bookmark_schedule_rows):
            if r["id"] not in wanted:
                continue
            cfg = resolve_schedule(r, self.folders, self.settings)
            if cfg is None or (not cfg.days and cfg.window is None):
                continue
            allowed = sched.apply_limits(cfg, now, self.zone)
            if allowed > now:
                moved[r["id"]] = allowed
        if moved:

            def store(conn: sqlite3.Connection) -> None:
                for bid, due in moved.items():
                    repo.bookmark_update(conn, bid, {"next_due_at": iso(due)}, iso(now))

            await self.db.write(store)
            for bid, due in moved.items():
                self.scheduler.reschedule(bid, due)
        n = self.scheduler.catch_up(self.settings.catchup_spread_s, skip=set(moved))
        log.info("catch_up", reason=reason, overdue=len(overdue), scheduled=n, moved=len(moved))
        if n:
            self.events.publish("engine_state", {"reason": "catch_up", "queued": n})
        return n

    # -- backup and restore -------------------------------------------------------------

    async def backup_now(
        self, *, include_blobs: bool | None = None, dest: Path | None = None,
        kind: backups.Kind = "manual",
    ) -> backups.BackupInfo:  # fmt: skip
        """Write a backup zip (a thread does the copying; checks keep running)."""
        blobs_too = self.settings.backup_include_blobs if include_blobs is None else include_blobs
        info = await asyncio.to_thread(
            backups.create_backup, self.db, self.data_dir, include_blobs=blobs_too,
            now=self.clock.now(), app_version=self.version, dest=dest, kind=kind,
        )  # fmt: skip
        if kind == "auto":
            await asyncio.to_thread(
                backups.prune_backups, self.data_dir.backups_dir, self.settings.backup_keep
            )
        return info

    async def restore(self, source: Path) -> backups.Manifest:
        """Validate and stage a backup, then restart: it is applied when the engine comes back
        (exit code 3: Task Scheduler restarts it, or the engine starts its own replacement)."""
        manifest = await asyncio.to_thread(
            backups.stage_restore, self.data_dir, source, self.clock.now()
        )
        self.spawn(self._restart_soon(), "restart-after-restore")
        return manifest

    async def _restart_soon(self) -> None:
        await asyncio.sleep(0.3)  # let the HTTP response reach the client first
        self.request_stop(EXIT_RESTART)

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
        )  # (update_settings re-applies the keep-awake option for the new state)

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
        rss_total = cpu = last_backup = last_maintenance = None
        if self.guard is not None:
            rss_total = round(await asyncio.to_thread(tree_rss_mb), 1)
            if self.guard.last_cpu_percent is not None:
                cpu = round(self.guard.last_cpu_percent, 2)
        if self.maintenance is not None:
            done = self.maintenance.last
            last_backup = iso(done["backup"]) if done["backup"] else None
            last_maintenance = iso(done["maintenance"]) if done["maintenance"] else None
        return HealthOut(
            version=self.version,
            uptime_s=max(0.0, self.clock.monotonic() - started),
            pid=psutil.Process().pid,
            queue_length=self.scheduler.queue_length,
            in_flight=self.scheduler.in_flight,
            outcomes_24h=outcomes,
            rss_mb=round(psutil.Process().memory_info().rss / 1_048_576, 1),
            browser_state=self.browser.state,
            autowatch=AutowatchState(
                state="paused" if self.scheduler.paused else "running",
                until=iso(until) if until else None,
            ),
            bookmarks=bookmarks,
            online=self.online,
            on_battery=self.on_battery,
            backlog_warning=self.scheduler.backlog_warning,
            rss_total_mb=rss_total,
            cpu_percent=cpu,
            last_backup_at=last_backup,
            last_maintenance_at=last_maintenance,
        )


__all__ = ["EXIT_ALREADY_RUNNING", "EXIT_ERROR", "EXIT_OK", "EXIT_RESTART", "Engine", "Trigger"]
