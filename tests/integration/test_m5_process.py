"""M5 acceptance with real engine processes: kill -9 in the middle of a check, restore with the
engine restarting itself, and the supervised/unsupervised difference.

These start real ``pagewatch-engine`` processes (worker threads, no tray) against a fixture web
server in this process. The engine's connectivity probe is pointed at that server too: the
default probe URL is somebody else's web site.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from pagewatch.engine import main as engine_main
from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.engine.store.db import migrate
from tests.support.fixture_site import FixtureSite

PAGE = "<html><body><p>The reading room opens at nine on weekdays and ten on weekends.</p></body></html>"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


# -- helpers ----------------------------------------------------------------------------------


def seed_settings(dd: DataDir, site: FixtureSite) -> None:
    """Settings a test engine needs, written before it first starts (as ``PUT /settings`` would)."""
    migrate(dd.db_path)
    values = {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
        "worker_processes": 1, "toast_coalesce_s": 0.0,
        "connectivity_url": site.url("/probe"),  # any HTTP reply means "online"
    }  # fmt: skip
    conn = sqlite3.connect(dd.db_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO setting(key, value_json) VALUES(?, ?)",
            [(k, json.dumps(v)) for k, v in values.items()],
        )
        conn.commit()
    finally:
        conn.close()


def spawn(dd: DataDir, *extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "pagewatch.engine.main", "--data-dir", str(dd.root),
         "--workers", "thread", "--no-console-log", "--no-tray", *extra],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


def wait_until(check: Callable[[], Any], timeout: float = 30.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait_for_engine(dd: DataDir, *, not_pids: tuple[int, ...] = (), pid: int | None = None) -> Any:
    """The lockfile of a *running* engine (a killed one leaves a stale file behind)."""

    def ready() -> Any:
        info = read_lockfile(dd)
        if info is None or info.pid in not_pids or (pid is not None and info.pid != pid):
            return None
        return info if psutil.pid_exists(info.pid) else None

    return wait_until(ready, 40, "the engine to start")


def api(dd: DataDir) -> httpx.Client:
    info = read_lockfile(dd)
    assert info is not None
    return httpx.Client(
        base_url=f"http://127.0.0.1:{info.port}",
        headers={"Authorization": f"Bearer {info.token}"},
        timeout=30,
    )


def sql(dd: DataDir, query: str, *args: Any) -> list[sqlite3.Row]:
    conn = sqlite3.connect(dd.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


def stop(proc: subprocess.Popen[bytes] | None, pid: int | None = None) -> None:
    """Whatever is left of an engine, gone (a test must not leave processes behind)."""
    if proc is not None and proc.poll() is None:
        proc.send_signal(signal.SIGTERM) if sys.platform != "win32" else proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if pid is not None and psutil.pid_exists(pid):
        with_proc = psutil.Process(pid)
        with_proc.terminate()
        try:
            with_proc.wait(15)
        except psutil.TimeoutExpired:
            with_proc.kill()


# -- kill -9 in the middle of a check ---------------------------------------------------------


@posix_only
async def test_kill_9_mid_check_then_restart_leaves_no_corrupt_rows_and_requeues_the_check(
    tmp_path: Path,
) -> None:
    site = await FixtureSite().start()
    dd = DataDir(tmp_path / "data").ensure()
    seed_settings(dd, site)
    site.set("/library", PAGE, delay=6)  # the page answers slowly: the check is in flight
    second: subprocess.Popen[bytes] | None = None
    first = spawn(dd)
    try:
        info = await asyncio.to_thread(wait_for_engine, dd, pid=first.pid)

        def add_bookmark() -> int:
            with api(dd) as c:
                r = c.post("/bookmarks", json={"url": site.url("/library"), "name": "Library",
                                               "schedule": {"interval_s": 60, "jitter_pct": 0}})  # fmt: skip
                assert r.status_code == 201, r.text
                return int(r.json()["id"])

        bid = await asyncio.to_thread(add_bookmark)
        assert info.pid == first.pid
        # the check has started and is waiting on the page
        await asyncio.to_thread(
            wait_until,
            lambda: sql(dd, "SELECT id FROM check_run WHERE finished_at IS NULL"),
            30,
            "a check to be in flight",
        )
        first.kill()  # SIGKILL: no shutdown, no WAL checkpoint, nothing is flushed
        await asyncio.to_thread(first.wait, 10)
        assert first.returncode == -signal.SIGKILL

        # what the dead process left behind: a database that opens, is consistent, and still
        # shows the check as started
        assert sql(dd, "PRAGMA integrity_check")[0][0] == "ok"
        assert sql(dd, "PRAGMA foreign_key_check") == []
        assert len(sql(dd, "SELECT id FROM check_run WHERE finished_at IS NULL")) == 1
        assert sql(dd, "SELECT COUNT(*) FROM version")[0][0] == 0  # nothing half-written

        site.set("/library", PAGE)  # the site is quick now
        second = spawn(dd)
        await asyncio.to_thread(wait_for_engine, dd, pid=second.pid)
        # the interrupted check is closed as an error, and the bookmark is checked again
        await asyncio.to_thread(
            wait_until,
            lambda: sql(dd, "SELECT id FROM check_run WHERE outcome='first'"),
            30,
            "the re-queued check to finish",
        )
        runs = sql(dd, "SELECT * FROM check_run WHERE bookmark_id=? ORDER BY id", bid)
        interrupted = [r for r in runs if r["reason"] == "interrupted"]
        assert len(interrupted) == 1
        assert interrupted[0]["outcome"] == "error" and interrupted[0]["finished_at"] is not None
        assert runs[-1]["outcome"] == "first" and runs[-1]["trigger"] == "catchup"  # re-queued
        assert sql(dd, "SELECT id FROM check_run WHERE finished_at IS NULL") == []
        row = sql(dd, "SELECT * FROM bookmark WHERE id=?", bid)[0]
        assert row["consecutive_errors"] == 0  # a crash is not the site's fault
        assert row["status"] == "ok" and row["latest_version_id"] is not None
        assert sql(dd, "PRAGMA integrity_check")[0][0] == "ok"
    finally:
        stop(first)
        stop(second)
        await site.stop()


# -- restore: the engine restarts itself --------------------------------------------------------


@posix_only
async def test_a_restore_restarts_an_unsupervised_engine_into_the_restored_data(
    tmp_path: Path,
) -> None:
    site = await FixtureSite().start()
    dd = DataDir(tmp_path / "data").ensure()
    seed_settings(dd, site)
    site.set("/a", PAGE)
    first = spawn(dd)
    new_pid: int | None = None
    try:
        await asyncio.to_thread(wait_for_engine, dd, pid=first.pid)

        def build_and_restore() -> None:
            with api(dd) as c:
                c.post("/bookmarks", json={"url": site.url("/a"), "name": "kept",
                                           "schedule": {"interval_s": 3600}})  # fmt: skip
                backup = c.post("/backup").json()["path"]
                c.post("/bookmarks", json={"url": site.url("/b"), "name": "added later",
                                           "schedule": {"interval_s": 3600}})  # fmt: skip
                r = c.post("/restore", json={"path": backup})
                assert r.status_code == 202, r.text

        await asyncio.to_thread(build_and_restore)
        # the first engine exits with code 3 ("restart me") ...
        assert await asyncio.to_thread(first.wait, 20) == 3
        # ... and, with nobody supervising it, started a replacement that applies the restore
        info = await asyncio.to_thread(wait_for_engine, dd, not_pids=(first.pid,))
        new_pid = info.pid

        def names() -> list[str]:
            with api(dd) as c:
                return [b["name"] for b in c.get("/bookmarks").json()["items"]]

        assert await asyncio.to_thread(names) == ["kept"]
        assert list((dd.backups_dir).glob("pre-restore-*.db"))
        assert not (dd.root / "restore" / "pending.zip").exists()
    finally:
        stop(first)
        stop(None, new_pid)
        await site.stop()


@posix_only
async def test_a_supervised_engine_exits_for_its_supervisor_instead_of_restarting_itself(
    tmp_path: Path,
) -> None:
    site = await FixtureSite().start()
    dd = DataDir(tmp_path / "data").ensure()
    seed_settings(dd, site)
    proc = spawn(dd, "--supervised")
    try:
        await asyncio.to_thread(wait_for_engine, dd, pid=proc.pid)

        def restore() -> None:
            with api(dd) as c:
                backup = c.post("/backup").json()["path"]
                assert c.post("/restore", json={"path": backup}).status_code == 202

        await asyncio.to_thread(restore)
        assert await asyncio.to_thread(proc.wait, 20) == 3  # Task Scheduler restarts on this
        await asyncio.sleep(4)
        assert read_lockfile(dd) is None  # nobody else started one
        assert (
            dd.root / "restore" / "pending.zip"
        ).exists()  # waiting for the supervisor's restart
    finally:
        stop(proc)
        await site.stop()


# -- the respawn guard (no process needed) -----------------------------------------------------


def test_an_engine_that_was_itself_just_restarted_does_not_restart_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(engine_main.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.delenv(engine_main.RESPAWN_ENV, raising=False)
    assert engine_main.respawn(["--data-dir", "x"]) is True
    assert started[0][-2:] == ["--data-dir", "x"] and "--supervised" not in started[0]
    monkeypatch.setenv(engine_main.RESPAWN_ENV, str(time.time() - 5))  # restarted 5 s ago
    assert engine_main.respawn(["--data-dir", "x"]) is False and len(started) == 1
    monkeypatch.setenv(engine_main.RESPAWN_ENV, str(time.time() - 120))  # a minute and more ago
    assert engine_main.respawn(["--data-dir", "x"]) is True and len(started) == 2
    monkeypatch.setenv(engine_main.RESPAWN_ENV, "garbage")
    assert engine_main.respawn([]) is True


def test_the_respawned_engine_is_a_separate_session_not_a_child_that_dies_with_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(engine_main.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    monkeypatch.delenv(engine_main.RESPAWN_ENV, raising=False)
    engine_main.respawn([])
    assert seen.get("start_new_session") is True or "creationflags" in seen
    assert seen["stdin"] == subprocess.DEVNULL and os.environ.get(engine_main.RESPAWN_ENV) is None
