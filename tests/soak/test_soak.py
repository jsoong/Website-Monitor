"""Soak tests (opt in: ``pytest -m soak tests/soak -s``).

Two stand-ins for the spec's 24-hour soak, which cannot run in a development sandbox:

* ``test_a_simulated_day_at_1000_bookmarks``: 24 *simulated* hours under a FakeClock with 1,000
  bookmarks at 1-60 minute intervals, through the real scheduler, runner, pipeline, retention,
  backup and metrics code. It proves the logic over a day: nothing stuck, no backlog, every
  bookmark checked on time, retention and the nightly jobs ran, no orphan blob left after GC,
  memory not growing. It cannot prove anything about wall-clock behaviour.
* ``test_real_engine_memory_and_idle_cpu``: a real engine process (real worker processes) with
  1,000 bookmarks for a few real minutes, measuring actual RSS and CPU against the spec's budgets.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import random
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from pagewatch.engine.actions.toast import Toast
from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import FakeClock, parse_iso
from pagewatch.engine.core import Engine
from pagewatch.engine.instance import read_lockfile, write_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.engine.power import NullPowerBackend
from pagewatch.engine.store.retention import collect_references
from pagewatch.models import LockInfo
from tests.support.fakes import ok
from tests.support.fixture_site import FixtureSite
from tests.support.sim import settle

pytestmark = pytest.mark.soak

MIB = 1024 * 1024
N = int(os.environ.get("PAGEWATCH_SOAK_BOOKMARKS", "1000"))  # the spec's design load
HOURS = int(os.environ.get("PAGEWATCH_SOAK_HOURS", "24"))  # simulated hours (the spec: 24)
# (interval seconds, share of the bookmarks): the spec's "1-60 minute intervals"
MIX = [(60, 0.005), (300, 0.045), (900, 0.15), (1800, 0.30), (3600, 0.50)]
COUNTS = [(interval, max(1, round(share * N))) for interval, share in MIX]


class CountingToasts:
    """Counts toasts and keeps none (``LogToastBackend`` keeps every one for assertions)."""

    def __init__(self) -> None:
        self.count = 0

    async def show(self, toast: Toast) -> None:
        self.count += 1


class QuickFetcher:
    """Answers from a function and remembers nothing. (``ScriptedFetcher`` keeps every request
    for assertions, each pinning its whole resolved configuration: over tens of thousands of
    checks that is a leak of the *test double*, which a first version of this soak mistook for
    the engine's.)"""

    def __init__(self, respond: Callable[[Any], Any]) -> None:
        self.respond = respond

    async def fetch(self, request: Any) -> Any:
        return self.respond(request)

    async def aclose(self) -> None:
        return None


def page(host: str, revision: int) -> str:
    lines = "".join(f"<p>{host} item {i} is listed for the week.</p>" for i in range(12))
    return f"<html><body><h1>{host}</h1>{lines}<p>Revision {revision} of the weekly notice.</p></body></html>"


def rss_mb() -> float:
    gc.collect()
    return float(psutil.Process().memory_info().rss) / MIB


async def test_a_simulated_day_at_1000_bookmarks(tmp_path: Path) -> None:
    # pytest keeps every log record of a test in memory, and the engine logs a line per check:
    # left on, that (hundreds of MB over a day) would be measured as the engine's memory.
    logging.disable(logging.INFO)
    clock = FakeClock()
    data = DataDir(tmp_path / "data").ensure()
    toasts = CountingToasts()
    eng = Engine(
        data, clock=clock, worker_mode="thread", toast_backend=toasts, rng=random.Random(11),
        enable_unattended=True, power_backend=NullPowerBackend(),
        probe=lambda url, timeout, proxy: asyncio.sleep(0, result=True),
        settings_overrides={
            "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
            "worker_processes": 3, "toast_coalesce_s": 0.0, "keep_changed_versions": 3,
            "rss_limit_mb": 100_000, "timezone": "UTC",
        },
    )  # fmt: skip
    started_at = clock.now()

    def respond(req: Any) -> Any:
        host = req.url.split("//")[1].split("/")[0]
        slot = int(host[1:].split(".")[0])
        # each page changes about every six hours, at a different moment for each bookmark
        hours = (clock.now() - started_at).total_seconds() / 3600
        return ok(req, page(host, int((hours + slot % 6) // 6)))

    eng.fetchers.replace("static", QuickFetcher(respond))
    await eng.start()
    server = ApiServer(eng)
    port = await server.start()
    write_lockfile(data, LockInfo(pid=os.getpid(), port=port, token=eng.token, version="t", started_at="now"))  # fmt: skip
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {eng.token}"},
            timeout=60,
        ) as c:
            ids: dict[int, int] = {}  # bookmark id -> interval
            slot = 0
            for interval, count in COUNTS:
                for _ in range(count):
                    r = await c.post("/bookmarks", json={
                        "url": f"http://s{slot}.example.test/", "name": f"s{slot}",
                        "schedule": {"interval_s": interval, "jitter_pct": 0},
                        "actions": {"actions": [{"type": "toast"}]},
                    })  # fmt: skip
                    assert r.status_code == 201, r.text
                    ids[r.json()["id"]] = interval
                    slot += 1
            await settle(eng, clock)

            samples: list[tuple[int, float, int, int]] = []  # (hour, rss_mb, queue, in_flight)
            max_queue = max_in_flight = 0
            wall = time.monotonic()
            for hour in range(1, HOURS + 1):
                for _ in range(120):  # 30 simulated seconds at a time
                    clock.advance(30)
                    await settle(eng, clock)
                    max_queue = max(max_queue, eng.scheduler.queue_length)
                    max_in_flight = max(max_in_flight, eng.scheduler.in_flight)
                samples.append(
                    (hour, rss_mb(), eng.scheduler.queue_length, eng.scheduler.in_flight)
                )
            took = time.monotonic() - wall
            print("\nRSS (MB) at the end of each simulated hour: "
                  + " ".join(f"{rss:.0f}" for _, rss, _, _ in samples))  # fmt: skip

            # -- nothing stuck, nothing queued up ---------------------------------------
            await settle(eng, clock)
            assert eng.scheduler.in_flight == 0
            assert max_queue < eng.settings.backlog_warning, f"queue peaked at {max_queue}"
            open_runs = await eng.db.read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM check_run WHERE finished_at IS NULL"
                ).fetchone()[0]
            )
            assert open_runs == 0

            # -- every bookmark was checked on time (nobody starved) --------------------
            rows = await eng.db.read(
                lambda conn: conn.execute(
                    "SELECT id, last_checked_at, status FROM bookmark"
                ).fetchall()
            )
            now = clock.now()
            for row in rows:
                late = now - parse_iso(row["last_checked_at"])
                assert late <= timedelta(seconds=ids[row["id"]] + 60), (row["id"], late)
                assert row["status"] in ("ok", "changed"), row["status"]
            counts = await eng.db.read(
                lambda conn: dict(
                    conn.execute(
                        "SELECT outcome, COUNT(*) FROM check_run GROUP BY outcome"
                    ).fetchall()
                )
            )
            total_checks = sum(counts.values())
            assert counts.get("error", 0) == 0 and total_checks > 50 * N

            # -- the nightly jobs ran -----------------------------------------------------
            backups = list(data.backups_dir.glob("backup-auto-*.zip"))
            assert len(backups) >= 1  # at 03:00 (the clock started at noon, so one night passed)
            # one right after the first start (it had never run), then one per 03:00 passed
            nights = (HOURS - 15) // 24 + 1 if HOURS >= 15 else 0
            assert len(backups) == min(1 + nights, eng.settings.backup_keep)
            assert eng.maintenance is not None and eng.maintenance.last_retention is not None
            versions = await eng.db.read(
                lambda conn: conn.execute("SELECT COUNT(*) FROM version").fetchone()[0]
            )
            assert versions <= len(ids) * (
                3 + 3
            )  # keep 3 per bookmark, plus pointer-referenced ones
            metrics = await eng.db.read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM metric WHERE name='rss_mb'"
                ).fetchone()[0]
            )
            assert metrics >= HOURS - 1  # an hourly sample

            # -- memory: no growth after warm-up -----------------------------------------
            warm = samples[1][1]  # after two simulated hours
            final = samples[-1][1]
            growth = (final - warm) / warm
            print(f"\nsimulated {HOURS} h: {total_checks:,} checks in {took:.0f} s real time; "
                  f"queue peak {max_queue}, in-flight peak {max_in_flight}; "
                  f"RSS {warm:.0f} MB after 2 h -> {final:.0f} MB after 24 h ({growth:+.1%}); "
                  f"{versions:,} versions kept")  # fmt: skip
            assert growth < 0.10, f"RSS grew {growth:.1%}"

            # -- no orphan blobs once the collector has had its way -----------------------
            old = clock.now().timestamp() - 48 * 3600
            for digest, _, _ in list(eng.blobs.iter_blob_files()):
                os.utime(eng.blobs.path_for(digest), (old, old))
            report = await eng.maintenance.retention.run()
            refs = await eng.db.read(lambda conn: collect_references(conn, eng.blobs))
            leftover = [d for d, _, _ in eng.blobs.iter_blob_files() if int(d[:16], 16) not in refs]
            assert leftover == [], f"{len(leftover)} orphan blobs survived GC"
            assert report.blobs_deleted > 0
            # and everything a version needs is still there
            for row in await eng.db.read(
                lambda conn: conn.execute("SELECT raw_hash, blocks_hash FROM version").fetchall()
            ):
                assert eng.blobs.exists(row["raw_hash"]) and eng.blobs.exists(row["blocks_hash"])
            check = await eng.db.read(
                lambda conn: conn.execute("PRAGMA integrity_check").fetchone()[0]
            )
            assert check == "ok"
    finally:
        await server.stop()
        await eng.stop()


# -- real process, real memory ------------------------------------------------------------------


def tree(proc: psutil.Process) -> list[psutil.Process]:
    return [proc, *proc.children(recursive=True)]


def tree_rss(proc: psutil.Process) -> tuple[float, float]:
    """(engine process MB, whole tree MB)."""
    own = proc.memory_info().rss / MIB
    return own, sum(p.memory_info().rss for p in tree(proc) if p.is_running()) / MIB


def cpu_seconds(proc: psutil.Process) -> float:
    total = 0.0
    for p in tree(proc):
        try:
            t = p.cpu_times()
            total += t.user + t.system
        except psutil.Error:
            continue
    return total


def spawn_engine(dd: DataDir) -> subprocess.Popen[bytes]:
    """The real thing: worker *processes*, as in production."""
    return subprocess.Popen(
        [sys.executable, "-m", "pagewatch.engine.main", "--data-dir", str(dd.root),
         "--no-console-log", "--no-tray"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


async def test_real_engine_memory_and_idle_cpu(tmp_path: Path) -> None:
    site = await FixtureSite().start()
    dd = DataDir(tmp_path / "data").ensure()
    from pagewatch.engine.store.db import migrate

    migrate(dd.db_path)
    settings = {
        "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 32,
        "worker_processes": 3, "toast_coalesce_s": 0.0, "connectivity_url": site.url("/probe"),
    }  # fmt: skip
    conn = sqlite3.connect(dd.db_path)
    conn.executemany(
        "INSERT INTO setting(key, value_json) VALUES(?,?)",
        [(k, json.dumps(v)) for k, v in settings.items()],
    )
    conn.commit()
    conn.close()
    for i in range(N):
        site.set(f"/p{i}", page(f"p{i}", 0))
    proc = spawn_engine(dd)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            info = read_lockfile(dd)
            if info is not None and info.pid == proc.pid:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("engine did not start")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{info.port}",
            headers={"Authorization": f"Bearer {info.token}"},
            timeout=60,
        ) as c:
            # 1,000 hourly bookmarks, as the spec's design load (the checks themselves are fast)
            for i in range(N):
                r = await c.post("/bookmarks", json={"url": site.url(f"/p{i}"), "name": f"p{i}",
                                                     "schedule": {"interval_s": 3600}})  # fmt: skip
                assert r.status_code == 201
            # the first wave of checks
            for _ in range(600):
                h = (await c.get("/health")).json()
                if h["outcomes_24h"].get("first", 0) >= N and h["in_flight"] == 0:
                    break
                await asyncio.sleep(0.5)
            assert h["outcomes_24h"].get("first", 0) >= N, h["outcomes_24h"]

            engine_proc = psutil.Process(proc.pid)
            await asyncio.sleep(5)
            own, total = tree_rss(engine_proc)
            print(f"\nreal engine, {N} bookmarks, browser closed: RSS {own:.0f} MB (engine process), "
                  f"{total:.0f} MB with {len(tree(engine_proc)) - 1} worker processes")  # fmt: skip

            # idle: AutoWatch paused, so no check is due
            await c.post("/autowatch", json={"state": "paused"})
            await asyncio.sleep(5)
            before, t0 = cpu_seconds(engine_proc), time.monotonic()
            await asyncio.sleep(60)
            idle = (cpu_seconds(engine_proc) - before) / (time.monotonic() - t0) * 100
            print(f"idle CPU, no checks due (AutoWatch paused): {idle:.2f}% of one core, "
                  f"{idle / (psutil.cpu_count() or 1):.2f}% of the machine")  # fmt: skip

            # steady state: the hourly checks trickle in (1,000/hour is under 0.3 checks/s)
            await c.post("/autowatch", json={"state": "running"})
            await asyncio.sleep(5)
            before, t0 = cpu_seconds(engine_proc), time.monotonic()
            await asyncio.sleep(60)
            busy = (cpu_seconds(engine_proc) - before) / (time.monotonic() - t0) * 100
            print(f"steady CPU, hourly bookmarks running: {busy:.2f}% of one core")
            assert idle < 1.0, f"idle CPU {idle:.2f}%"
            h = (await c.get("/health")).json()
            assert h["rss_total_mb"] is not None and h["bookmarks"] == N
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
        await site.stop()
