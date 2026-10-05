"""SQLite access: migrations, one writer thread, a small pool of reader threads.

``sqlite3`` calls block, so the event loop never touches a connection. Writes are queued
to a single dedicated thread that owns the only write connection; every write callable
runs inside one ``BEGIN IMMEDIATE`` transaction (so a check's writes commit atomically).
Reads run on their own connections in a thread pool and see the WAL snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import re
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

_MIGRATION_NAME = re.compile(r"^(\d{4})_[\w-]+\.sql$")


class SchemaTooNew(RuntimeError):
    """The database was written by a newer PageWatch than this one."""


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_size_limit=67108864")
    return conn


# -- migrations -------------------------------------------------------------------------


def _migration_files() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    root = resources.files("pagewatch.engine.store") / "migrations"
    for entry in root.iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


def latest_schema_version() -> int:
    files = _migration_files()
    return files[-1][0] if files else 0


def current_schema_version(conn: sqlite3.Connection) -> int:
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not has:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def backup_database(src: sqlite3.Connection, dest: Path) -> None:
    """Online backup (consistent snapshot even while the writer is active)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()


def migrate(db_path: Path, backups_dir: Path | None = None) -> int:
    """Apply pending migrations in one transaction, after an automatic backup of an
    existing database. Returns the resulting schema version."""
    files = _migration_files()
    conn = connect(db_path)
    try:
        current = current_schema_version(conn)
        latest = files[-1][0] if files else 0
        if current > latest:
            raise SchemaTooNew(
                f"database schema v{current} is newer than this build's v{latest}; "
                "install a newer PageWatch"
            )
        pending = [(n, sql) for n, sql in files if n > current]
        if not pending:
            return current
        if current > 0 and backups_dir is not None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_database(conn, backups_dir / f"pre-migration-v{current}-{stamp}.db")
        parts = ["BEGIN;"]
        for number, sql in pending:
            parts.append(sql)
            parts.append(
                f"DELETE FROM schema_version; INSERT INTO schema_version VALUES({number});"
            )
        parts.append("COMMIT;")
        try:
            conn.executescript("\n".join(parts))
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return latest
    finally:
        conn.close()


# -- database ---------------------------------------------------------------------------


class Database:
    def __init__(self, path: Path, *, readers: int = 4) -> None:
        self.path = path
        self._n_readers = readers
        self._wq: queue.SimpleQueue[tuple[Callable[[sqlite3.Connection], Any], Future[Any]] | None]
        self._wq = queue.SimpleQueue()
        self._writer: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._readers: ThreadPoolExecutor | None = None
        self._local = threading.local()
        self._reader_conns: list[sqlite3.Connection] = []
        self._reader_lock = threading.Lock()
        self._closed = False

    # lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        if self._writer is not None:
            return
        self._writer = threading.Thread(target=self._writer_main, name="pw-writer", daemon=True)
        self._writer.start()
        self._ready.wait()
        if self._start_error is not None:
            raise self._start_error
        self._readers = ThreadPoolExecutor(
            max_workers=self._n_readers, thread_name_prefix="pw-read"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._wq.put(None)
            self._writer.join(timeout=30)
        if self._readers is not None:
            self._readers.shutdown(wait=True)
        with self._reader_lock:
            for conn in self._reader_conns:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            self._reader_conns.clear()

    # writer ---------------------------------------------------------------------------

    def _writer_main(self) -> None:
        try:
            conn = connect(self.path)
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                job = self._wq.get()
                if job is None:
                    break
                fn, fut = job
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(conn)
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
                    conn.execute("COMMIT")
                except BaseException as exc:
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()

    def _submit(self, fn: Callable[[sqlite3.Connection], T]) -> Future[T]:
        if self._closed:
            raise RuntimeError("database is closed")
        fut: Future[T] = Future()
        self._wq.put((fn, fut))
        return fut

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` in one transaction on the writer thread."""
        return await asyncio.wrap_future(self._submit(fn))

    def write_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self._submit(fn).result()

    @property
    def write_queue_depth(self) -> int:
        return self._wq.qsize()

    # readers --------------------------------------------------------------------------

    def _reader_conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path, readonly=True)
            self._local.conn = conn
            with self._reader_lock:
                self._reader_conns.append(conn)
        return conn

    def _run_read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._reader_conn()
        conn.execute("BEGIN")  # one consistent snapshot per call
        try:
            return fn(conn)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("COMMIT")

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._readers is None:
            raise RuntimeError("database not started")
        return await asyncio.get_running_loop().run_in_executor(self._readers, self._run_read, fn)

    def read_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._readers is None:
            raise RuntimeError("database not started")
        return self._readers.submit(self._run_read, fn).result()

    # online backup --------------------------------------------------------------------

    def backup_sync(self, dest: Path) -> None:
        def run(conn: sqlite3.Connection) -> None:
            backup_database(conn, dest)

        self.read_sync(run)
