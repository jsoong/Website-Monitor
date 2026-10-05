"""Retention and garbage collection (spec: Data model -> Retention).

Nightly the engine

1. prunes versions: every version a bookmark pointer references, every pinned version and the
   newest ``keep_changed_versions`` per bookmark stay; the rest go, with the ``change`` rows (and
   their action jobs) that point at them;
2. collects blobs: everything no row references any more, *including blobs referenced from inside
   a screenshot diff*, is deleted if it has not been used for ``blob_grace_h`` hours;
3. enforces the disk cap by pruning the oldest unpinned, unreferenced versions first;
4. deletes ``check_run`` and ``metric`` rows past their retention and checkpoints the WAL.

Why the collector cannot delete something that is about to be used. A worker writes a blob
*before* the database row that references it, and skips the write when the blob already exists
(a dedupe hit, which refreshes the blob's modification time: see ``BlobStore``). So a blob that a
check has just produced or reused is always younger than the grace period, whatever the
collector's snapshot of the references says. A mark phase that fails stops the whole run: nothing
is swept from a reference set that may be incomplete.

The scans that read whole tables run on a short-lived connection of their own: through the
reader pool they would leave copies of those tables in the readers' page caches, one reader per
night, which shows up as memory that creeps up for a week and then stops.

The reference set keeps only the first 64 bits of each digest. A collision can only make an
unreferenced blob look referenced (it is kept one more night), never the other way round.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TypeVar

import zstandard

from pagewatch.engine.clock import Clock, iso
from pagewatch.engine.logs import get_logger
from pagewatch.engine.store.blobs import BlobNotFound, BlobStore
from pagewatch.engine.store.db import Database, connect
from pagewatch.models import Settings

log = get_logger("pagewatch.retention")

T = TypeVar("T")

CHUNK = 200  # versions deleted per write transaction: the writer thread is never held for long
LOG_CHUNK = 5000
TEMP_STALE_S = 3600.0  # a leftover ``.tmp`` this old is from a writer that died
MAX_CAP_ROUNDS = 5
_SCREENSHOT_DIFF = b'{"type":"screenshot"'  # how ``put_json`` starts the screenshot diff payload
_NESTED_KEYS = ("old", "new", "overlay")


@dataclass(slots=True)
class RetentionReport:
    versions_pruned: int = 0
    changes_pruned: int = 0
    blobs_deleted: int = 0
    bytes_freed: int = 0
    temp_files_deleted: int = 0
    check_runs_deleted: int = 0
    metrics_deleted: int = 0
    blob_bytes: int = 0  # what is left
    disk_cap_pruned: int = 0  # versions pruned only because of the cap
    disk_cap_unreachable: bool = False
    skipped: str | None = None  # why nothing was swept

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _prefix(digest: str) -> int:
    return int(digest[:16], 16)


# -- the reference set ------------------------------------------------------------------------


def _nested_refs(blobs: BlobStore, digest: str) -> list[str]:
    """Blobs a diff blob points at. Only the screenshot diff has any (the two screenshots and
    the overlay); it is recognised from its first bytes so a huge text diff is never read."""
    try:
        if blobs.peek(digest, len(_SCREENSHOT_DIFF)) != _SCREENSHOT_DIFF:
            return []
        payload = blobs.get_json(digest)
    except (BlobNotFound, ValueError, zstandard.ZstdError):
        return []
    if not isinstance(payload, dict):
        return []
    return [h for k in _NESTED_KEYS if isinstance(h := payload.get(k), str) and len(h) == 64]


def collect_references(conn: sqlite3.Connection, blobs: BlobStore) -> set[int]:
    """Every blob some row refers to. Runs on a reader connection (one consistent snapshot)."""
    refs: set[int] = set()
    for row in conn.execute("SELECT raw_hash, blocks_hash, screenshot_hash FROM version"):
        for digest in row:
            if digest:
                refs.add(_prefix(digest))
    for (digest,) in conn.execute(
        "SELECT diff_hash FROM change WHERE diff_hash IS NOT NULL "
        "UNION SELECT diff_hash FROM view_diff_cache"
    ):
        refs.add(_prefix(digest))
        for nested in _nested_refs(blobs, digest):
            refs.add(_prefix(nested))
    return refs


@dataclass(slots=True)
class SweepResult:
    deleted: int = 0
    freed: int = 0
    kept_bytes: int = 0
    temp_deleted: int = 0


def sweep(blobs: BlobStore, refs: set[int], cutoff_epoch: float) -> SweepResult:
    """Delete unreferenced blobs not used since ``cutoff_epoch``; report what remains."""
    out = SweepResult()
    for digest, size, mtime in blobs.iter_blob_files():
        if _prefix(digest) in refs or mtime > cutoff_epoch:
            out.kept_bytes += size
            continue
        try:
            freed = blobs.delete(digest)
        except OSError:  # open elsewhere (Windows) or a permissions problem: next night
            out.kept_bytes += size
            continue
        out.deleted += 1
        out.freed += freed
    for path in blobs.iter_stale_temp_files(TEMP_STALE_S):
        with contextlib.suppress(OSError):
            path.unlink()
            out.temp_deleted += 1
    return out


# -- version pruning --------------------------------------------------------------------------

_POINTERS = (
    "SELECT latest_version_id FROM bookmark WHERE latest_version_id IS NOT NULL "
    "UNION SELECT baseline_version_id FROM bookmark WHERE baseline_version_id IS NOT NULL "
    "UNION SELECT gate_anchor_version_id FROM bookmark WHERE gate_anchor_version_id IS NOT NULL"
)


def over_keep_limit(conn: sqlite3.Connection, keep: int) -> list[int]:
    """Versions beyond each bookmark's newest ``keep`` that no pointer references and nobody
    pinned. (Re-checked inside the delete: a pointer or a pin may have moved meanwhile.)"""
    rows = conn.execute(
        "SELECT id FROM (SELECT id, pinned, ROW_NUMBER() OVER ("
        "PARTITION BY bookmark_id ORDER BY fetched_at DESC, id DESC) AS rn FROM version) "
        f"WHERE rn > ? AND pinned = 0 AND id NOT IN ({_POINTERS})",
        (keep,),
    ).fetchall()
    return [r[0] for r in rows]


def oldest_unreferenced(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """The oldest unpinned versions no pointer references (disk-cap candidates)."""
    return conn.execute(
        "SELECT id, raw_hash, blocks_hash, screenshot_hash FROM version "
        f"WHERE pinned = 0 AND id NOT IN ({_POINTERS}) ORDER BY fetched_at ASC, id ASC LIMIT ?",
        (limit,),
    ).fetchall()


def delete_versions(conn: sqlite3.Connection, ids: Sequence[int]) -> tuple[int, int]:
    """Delete the still-eligible ones among ``ids`` and the changes that point at them (their
    jobs cascade). Returns ``(versions, changes)``. Runs in the writer's transaction."""
    if not ids:
        return 0, 0
    marks = ",".join("?" * len(ids))
    eligible = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM version WHERE id IN ({marks}) AND pinned = 0 "
            f"AND id NOT IN ({_POINTERS})",
            list(ids),
        )
    ]
    if not eligible:
        return 0, 0
    m = ",".join("?" * len(eligible))
    changes = conn.execute(
        f"DELETE FROM change WHERE old_version_id IN ({m}) OR new_version_id IN ({m})",
        eligible + eligible,
    ).rowcount
    conn.execute(
        f"DELETE FROM view_diff_cache WHERE baseline_version_id IN ({m}) "
        f"OR latest_version_id IN ({m})",
        eligible + eligible,
    )
    versions = conn.execute(f"DELETE FROM version WHERE id IN ({m})", eligible).rowcount
    return versions, changes


# -- the job ----------------------------------------------------------------------------------


class Retention:
    def __init__(
        self, db: Database, blobs: BlobStore, clock: Clock, settings: Callable[[], Settings]
    ) -> None:
        self.db = db
        self.blobs = blobs
        self.clock = clock
        self._settings = settings

    async def run(self) -> RetentionReport:
        s = self._settings()
        report = RetentionReport()
        report.versions_pruned, report.changes_pruned = await self._prune(
            await self._scan(lambda c: over_keep_limit(c, s.keep_changed_versions))
        )
        try:
            result = await self._collect(s.blob_grace_h * 3600.0)
        except Exception:
            # The reference set may be incomplete: sweep nothing.
            log.exception("blob_gc_aborted")
            report.skipped = "the reference scan failed"
            result = None
        if result is not None:
            report.blobs_deleted, report.bytes_freed = result.deleted, result.freed
            report.temp_files_deleted, report.blob_bytes = result.temp_deleted, result.kept_bytes
            await self._enforce_cap(report, s)
        now = self.clock.now()
        report.check_runs_deleted = await self._purge(
            "DELETE FROM check_run WHERE id IN (SELECT id FROM check_run WHERE started_at < ? "
            "LIMIT ?)", iso(now - timedelta(days=s.check_run_retention_days)),
        )  # fmt: skip
        report.metrics_deleted = await self._purge(
            "DELETE FROM metric WHERE rowid IN (SELECT rowid FROM metric WHERE ts < ? LIMIT ?)",
            iso(now - timedelta(days=s.metric_retention_days)),
        )  # fmt: skip
        await asyncio.to_thread(self._checkpoint)
        log.info("retention_done", **report.as_dict())
        return report

    # -- steps --------------------------------------------------------------------------

    async def _scan(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a read-only scan on a connection of its own (one consistent snapshot), closed
        afterwards, in a thread."""

        def run() -> T:
            conn = connect(self.db.path, readonly=True)
            try:
                conn.execute("BEGIN")
                try:
                    return fn(conn)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("COMMIT")
            finally:
                conn.close()

        return await asyncio.to_thread(run)

    async def _prune(self, ids: Sequence[int]) -> tuple[int, int]:
        versions = changes = 0
        for i in range(0, len(ids), CHUNK):
            chunk = list(ids[i : i + CHUNK])

            def delete_chunk(conn: sqlite3.Connection, chunk: list[int] = chunk) -> tuple[int, int]:
                return delete_versions(conn, chunk)

            v, c = await self.db.write(delete_chunk)
            versions, changes = versions + v, changes + c
        return versions, changes

    async def _collect(self, grace_s: float) -> SweepResult:
        refs = await self._scan(lambda c: collect_references(c, self.blobs))
        cutoff = self.clock.now().timestamp() - grace_s
        return await asyncio.to_thread(sweep, self.blobs, refs, cutoff)

    async def _enforce_cap(self, report: RetentionReport, s: Settings) -> None:
        """Over the disk cap: prune the oldest unpinned versions until enough blob bytes would
        be released, collect, and look again (blobs shared with a kept version do not free
        anything, so one round may not be enough)."""
        cap = int(s.disk_cap_gb * 1024**3)
        for _ in range(MAX_CAP_ROUNDS):
            excess = report.blob_bytes - cap
            if excess <= 0:
                return
            candidates = await self._scan(lambda c: oldest_unreferenced(c, 5000))
            chosen = await asyncio.to_thread(self._choose, candidates, excess)
            if not chosen:
                report.disk_cap_unreachable = True
                log.warning("disk_cap_unreachable", blob_bytes=report.blob_bytes, cap=cap)
                return
            versions, changes = await self._prune(chosen)
            report.versions_pruned += versions
            report.changes_pruned += changes
            report.disk_cap_pruned += versions
            result = await self._collect(s.blob_grace_h * 3600.0)
            report.blobs_deleted += result.deleted
            report.bytes_freed += result.freed
            report.blob_bytes = result.kept_bytes
        report.disk_cap_unreachable = report.blob_bytes > cap

    def _choose(self, candidates: Sequence[sqlite3.Row], excess: int) -> list[int]:
        """Oldest first, until their blobs add up to ``excess`` (an upper bound on what pruning
        them frees, since another version may share a blob)."""
        chosen: list[int] = []
        seen: set[str] = set()
        estimate = 0
        for row in candidates:
            for digest in (row["raw_hash"], row["blocks_hash"], row["screenshot_hash"]):
                if digest and digest not in seen:
                    seen.add(digest)
                    with contextlib.suppress(OSError, ValueError):
                        estimate += self.blobs.path_for(digest).stat().st_size
            chosen.append(row["id"])
            if estimate >= excess:
                break
        return chosen

    async def _purge(self, sql: str, cutoff: str) -> int:
        def delete_chunk(conn: sqlite3.Connection) -> int:
            return conn.execute(sql, (cutoff, LOG_CHUNK)).rowcount

        total = 0
        while True:
            n = await self.db.write(delete_chunk)
            total += n
            if n < LOG_CHUNK:
                return total

    def _checkpoint(self) -> None:
        """Let the WAL shrink. A passive checkpoint never waits for readers or the writer."""
        with contextlib.suppress(sqlite3.Error):
            conn = connect(self.db.path)
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                conn.close()
