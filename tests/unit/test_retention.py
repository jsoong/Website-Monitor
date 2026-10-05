"""Retention and garbage collection: what is kept matters more than what is deleted."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine.clock import FakeClock, iso
from pagewatch.engine.store import retention
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.engine.store.db import Database, migrate
from pagewatch.engine.store.retention import Retention
from pagewatch.models import Settings

HOUR = 3600.0


class Rig:
    def __init__(
        self, root: Path, db: Database, clock: FakeClock, settings: dict[str, Any]
    ) -> None:
        self.root = root
        self.db = db
        self.clock = clock
        self.blobs = BlobStore(root / "blobs")
        self.settings = Settings.model_validate(settings)
        self.ret = Retention(db, self.blobs, clock, lambda: self.settings)
        self._n = 0

    # -- building a database ------------------------------------------------------------

    async def bookmark(self, name: str = "b") -> int:
        now = iso(self.clock.now())

        def w(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO bookmark(name,url,schedule_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?)", (name, f"http://{name}.test/", "{}", now, now),
            )  # fmt: skip
            assert cur.lastrowid is not None
            return cur.lastrowid

        return await self.db.write(w)

    async def version(
        self, bid: int, n: int, *, pinned: int = 0, shot: bool = False, raw: bytes | None = None
    ) -> int:  # fmt: skip
        raw_hash = self.blobs.put(raw if raw is not None else f"raw {bid} {n}".encode() * 40)
        blocks_hash = self.blobs.put_json([f"block {bid} {n}"] * 20)
        shot_hash = self.blobs.put(os.urandom(300)) if shot else None
        at = iso(self.clock.now() - timedelta(days=60) + timedelta(minutes=n))

        def w(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO version(bookmark_id,fetched_at,raw_hash,blocks_hash,filtered_hash,"
                "screenshot_hash,pinned) VALUES(?,?,?,?,?,?,?)",
                (bid, at, raw_hash, blocks_hash, f"f{bid}-{n}", shot_hash, pinned),
            )  # fmt: skip
            assert cur.lastrowid is not None
            return cur.lastrowid

        return await self.db.write(w)

    async def change(
        self, bid: int, old: int | None, new: int, diff: str | None = None, job: bool = False
    ) -> int:  # fmt: skip
        at = iso(self.clock.now())

        def w(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO change(bookmark_id,old_version_id,new_version_id,detected_at,"
                "diff_hash) VALUES(?,?,?,?,?)", (bid, old, new, at, diff),
            )  # fmt: skip
            assert cur.lastrowid is not None
            if job:
                c.execute("INSERT INTO action_job(change_id,action_index,action_type,status) "
                          "VALUES(?,0,'toast','queued')", (cur.lastrowid,))  # fmt: skip
            return cur.lastrowid

        return await self.db.write(w)

    async def point(self, bid: int, latest: int, baseline: int | None, anchor: int | None) -> None:
        await self.db.write(
            lambda c: c.execute(
                "UPDATE bookmark SET latest_version_id=?, baseline_version_id=?, "
                "gate_anchor_version_id=? WHERE id=?",
                (latest, baseline, anchor, bid),
            )  # fmt: skip
        )

    # -- looking --------------------------------------------------------------------------

    async def ids(self, table: str, col: str = "id") -> set[int]:
        rows = await self.db.read(lambda c: c.execute(f"SELECT {col} FROM {table}").fetchall())
        return {r[0] for r in rows}

    def age(self, digest: str, hours: float) -> None:
        t = self.clock.now().timestamp() - hours * HOUR
        os.utime(self.blobs.path_for(digest), (t, t))

    def age_all(self, hours: float = 48) -> None:
        for digest, _, _ in list(self.blobs.iter_blob_files()):
            self.age(digest, hours)

    def digests(self) -> set[str]:
        return {d for d, _, _ in self.blobs.iter_blob_files()}


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    path = tmp_path / "pagewatch.db"
    migrate(path)
    db = Database(path)
    db.start()
    r = Rig(tmp_path, db, FakeClock(), {"keep_changed_versions": 5})
    try:
        yield r
    finally:
        db.close()


async def history(rig: Rig, n: int = 30) -> tuple[int, list[int]]:
    bid = await rig.bookmark()
    vids = [await rig.version(bid, i) for i in range(1, n + 1)]
    for old, new in zip(vids, vids[1:], strict=False):
        await rig.change(bid, old, new)
    return bid, vids


# -- version pruning --------------------------------------------------------------------------


async def test_keeps_the_newest_n_the_pointers_and_pinned_versions(rig: Rig) -> None:
    bid = await rig.bookmark()
    vids = [await rig.version(bid, i, pinned=int(i == 7)) for i in range(1, 31)]
    await rig.point(bid, latest=vids[29], baseline=vids[2], anchor=vids[9])
    report = await rig.ret.run()
    kept = await rig.ids("version")
    expected = {vids[i] for i in (25, 26, 27, 28, 29)}  # the newest five
    expected |= {vids[29], vids[2], vids[9], vids[6]}  # latest, baseline, anchor, the pinned one
    assert kept == expected
    assert report.versions_pruned == 30 - len(expected)


async def test_a_change_goes_with_either_of_its_versions_and_its_jobs_cascade(rig: Rig) -> None:
    bid = await rig.bookmark()
    vids = [await rig.version(bid, i) for i in range(1, 13)]
    await rig.point(bid, vids[-1], vids[-1], vids[-1])
    keep_change = await rig.change(bid, vids[10], vids[11], job=True)  # both versions kept
    straddle = await rig.change(bid, vids[1], vids[8], job=True)  # old pruned, new kept
    straddle2 = await rig.change(bid, vids[8], vids[2], job=True)  # new pruned, old kept
    gone = await rig.change(bid, None, vids[0], job=True)  # a first-check change, pruned
    report = await rig.ret.run()
    assert await rig.ids("change") == {keep_change}, report
    assert straddle and straddle2 and gone
    assert len(await rig.ids("action_job")) == 1  # the others' jobs were deleted with them


async def test_a_second_run_changes_nothing(rig: Rig) -> None:
    bid, vids = await history(rig)
    await rig.point(bid, vids[-1], vids[-1], vids[-1])
    await rig.ret.run()
    again = await rig.ret.run()
    assert (again.versions_pruned, again.changes_pruned, again.blobs_deleted) == (0, 0, 0)


async def test_a_pointer_that_moved_after_the_scan_protects_its_version(rig: Rig) -> None:
    """The candidate list is a snapshot; the delete re-checks, in its own transaction."""
    bid = await rig.bookmark()
    vids = [await rig.version(bid, i) for i in range(1, 11)]
    await rig.point(bid, vids[-1], vids[-1], vids[-1])
    candidates = await rig.db.read(lambda c: retention.over_keep_limit(c, 5))
    assert vids[0] in candidates
    await rig.point(bid, vids[-1], baseline=vids[0], anchor=vids[-1])  # the user reads, say
    pruned, _ = await rig.ret._prune(candidates)
    assert vids[0] in await rig.ids("version") and pruned == len(candidates) - 1


# -- blob collection --------------------------------------------------------------------------


async def test_blobs_of_pruned_versions_go_and_shared_or_kept_ones_stay(rig: Rig) -> None:
    bid = await rig.bookmark()
    shared = b"identical content on two versions " * 20
    old = await rig.version(bid, 1, raw=shared)
    vids = [old] + [await rig.version(bid, i) for i in range(2, 9)]
    newest = await rig.version(bid, 9, raw=shared)  # the newest version reuses an old blob
    await rig.point(bid, newest, newest, newest)
    rig.age_all()
    before = rig.digests()
    report = await rig.ret.run()
    after = rig.digests()
    assert vids[0] not in await rig.ids("version")
    shared_hash = rig.blobs.put(shared)
    assert shared_hash in after  # still referenced by the newest version
    for vid in await rig.ids("version"):  # every blob a surviving version needs is there
        row = await rig.db.read(lambda c, vid=vid: c.execute(
            "SELECT raw_hash, blocks_hash FROM version WHERE id=?", (vid,)).fetchone())  # fmt: skip
        assert rig.blobs.exists(row["raw_hash"]) and rig.blobs.exists(row["blocks_hash"])
    assert len(before) - len(after) == report.blobs_deleted > 0


async def test_an_unreferenced_blob_survives_until_it_has_been_unused_for_the_grace_period(
    rig: Rig,
) -> None:
    orphan_old = rig.blobs.put(b"orphan, old " * 50)
    orphan_new = rig.blobs.put(b"orphan, new " * 50)
    rig.age(orphan_old, 25)  # grace is 24 h
    rig.age(orphan_new, 23)
    report = await rig.ret.run()
    assert not rig.blobs.exists(orphan_old) and rig.blobs.exists(orphan_new)
    assert report.blobs_deleted == 1


async def test_reusing_a_blob_refreshes_it_so_a_collector_cannot_take_it_from_a_worker(
    rig: Rig,
) -> None:
    """The race the grace period exists for: a worker's dedupe hit on a blob that nothing
    references (its version was just pruned) must make the blob safe again."""
    content = b"page that came back " * 50
    digest = rig.blobs.put(content)
    rig.age(digest, 500)  # ancient and unreferenced: the collector would take it
    assert rig.blobs.put(content) == digest  # a check writes the same page again
    report = await rig.ret.run()  # (its version row is not committed yet)
    assert rig.blobs.exists(digest) and report.blobs_deleted == 0


async def test_blobs_referenced_from_inside_a_screenshot_diff_are_kept(rig: Rig) -> None:
    bid = await rig.bookmark()
    old = await rig.version(bid, 1, shot=True)
    new = await rig.version(bid, 2, shot=True)
    await rig.point(bid, new, new, new)
    overlay = rig.blobs.put(os.urandom(200))
    payload = {"type": "screenshot", "old": "0" * 64, "new": "1" * 64, "overlay": overlay,
               "ratio": 0.1}  # fmt: skip
    diff = rig.blobs.put_json(payload)
    await rig.change(bid, old, new, diff=diff)
    cache = rig.blobs.put_json([["eq", 0, 1]])
    await rig.db.write(lambda c: c.execute(
        "INSERT INTO view_diff_cache VALUES(?,?,?,?,?)", (bid, old, new, cache, "now")))  # fmt: skip
    rig.age_all()
    await rig.ret.run()
    assert rig.blobs.exists(overlay) and rig.blobs.exists(diff) and rig.blobs.exists(cache)


async def test_a_text_diff_is_not_read_and_its_blob_is_kept_while_its_change_lives(
    rig: Rig,
) -> None:
    bid = await rig.bookmark()
    v1, v2 = await rig.version(bid, 1), await rig.version(bid, 2)
    await rig.point(bid, v2, v2, v2)
    diff = rig.blobs.put_json({"ops": [["eq", 0, 5]] * 1000, "type": "text"})
    await rig.change(bid, v1, v2, diff=diff)
    rig.age_all()
    await rig.ret.run()
    assert rig.blobs.exists(diff)


async def test_the_overlay_goes_when_its_change_is_pruned(rig: Rig) -> None:
    bid = await rig.bookmark()
    vids = [await rig.version(bid, i, shot=True) for i in range(1, 11)]
    await rig.point(bid, vids[-1], vids[-1], vids[-1])
    overlay = rig.blobs.put(os.urandom(200))
    diff = rig.blobs.put_json({"type": "screenshot", "old": "0" * 64, "new": "1" * 64,
                               "overlay": overlay})  # fmt: skip
    await rig.change(bid, vids[0], vids[1], diff=diff)  # both versions are pruned
    rig.age_all()
    await rig.ret.run()
    assert not rig.blobs.exists(overlay) and not rig.blobs.exists(diff)


async def test_stale_temp_files_are_removed_and_fresh_ones_left(rig: Rig) -> None:
    digest = rig.blobs.put(b"x" * 100)
    folder = rig.blobs.path_for(digest).parent
    stale, fresh = folder / "a.tmp", folder / "b.tmp"
    stale.write_bytes(b"1")
    fresh.write_bytes(b"2")
    old = os.stat(stale).st_mtime - 2 * HOUR
    os.utime(stale, (old, old))
    report = await rig.ret.run()
    assert not stale.exists() and fresh.exists() and report.temp_files_deleted == 1


async def test_a_failing_reference_scan_sweeps_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = rig.blobs.put(b"orphan " * 50)
    rig.age(orphan, 100)

    def boom(conn: sqlite3.Connection, blobs: BlobStore) -> set[int]:
        raise RuntimeError("scan failed")

    monkeypatch.setattr(retention, "collect_references", boom)
    report = await rig.ret.run()
    assert rig.blobs.exists(orphan) and report.skipped and report.blobs_deleted == 0


# -- the disk cap -----------------------------------------------------------------------------


async def test_over_the_disk_cap_the_oldest_unpinned_versions_go_first(rig: Rig) -> None:
    rig.settings = Settings.model_validate({"keep_changed_versions": 100, "disk_cap_gb": 0.00002})
    bid = await rig.bookmark()
    pinned = await rig.version(bid, 1, pinned=1, raw=os.urandom(3000))
    vids = [pinned] + [await rig.version(bid, i, raw=os.urandom(3000)) for i in range(2, 21)]
    await rig.point(bid, vids[-1], vids[-1], vids[-1])
    rig.age_all()
    report = await rig.ret.run()
    kept = await rig.ids("version")
    cap = int(rig.settings.disk_cap_gb * 1024**3)
    assert report.blob_bytes <= cap and report.disk_cap_pruned > 0
    assert vids[0] in kept and vids[-1] in kept  # pinned and pointed-at stay
    gone = [v for v in vids if v not in kept]
    assert gone == vids[1 : 1 + len(gone)]  # the oldest unpinned, in order
    assert not report.disk_cap_unreachable


async def test_when_pointers_alone_exceed_the_cap_it_says_so_and_deletes_nothing_else(
    rig: Rig,
) -> None:
    rig.settings = Settings.model_validate({"keep_changed_versions": 100, "disk_cap_gb": 0.000001})
    bid = await rig.bookmark()
    v = await rig.version(bid, 1, raw=os.urandom(5000))
    await rig.point(bid, v, v, v)
    rig.age_all()
    report = await rig.ret.run()
    assert report.disk_cap_unreachable and await rig.ids("version") == {v}
    assert rig.blobs.exists(
        (await rig.db.read(lambda c: c.execute("SELECT raw_hash FROM version").fetchone()))[0]
    )


# -- logs and metrics -------------------------------------------------------------------------


async def test_check_runs_and_metrics_past_their_retention_are_deleted(rig: Rig) -> None:
    bid = await rig.bookmark()
    now = rig.clock.now()

    def seed(c: sqlite3.Connection) -> None:
        for days in (45, 31, 29, 1):
            at = iso(now - timedelta(days=days))
            c.execute("INSERT INTO check_run(bookmark_id,started_at,trigger,method,outcome) "
                      "VALUES(?,?, 'schedule','static','unchanged')", (bid, at))  # fmt: skip
            c.execute("INSERT INTO metric VALUES(?, 'rss_mb', 100)", (at,))

    await rig.db.write(seed)
    report = await rig.ret.run()
    assert report.check_runs_deleted == 2 and report.metrics_deleted == 2
    left = await rig.db.read(
        lambda c: [r[0] for r in c.execute("SELECT started_at FROM check_run")]
    )
    assert len(left) == 2 and all(s > iso(now - timedelta(days=30)) for s in left)


async def test_the_database_stays_consistent_after_every_kind_of_pruning(rig: Rig) -> None:
    bid, vids = await history(rig, 40)
    await rig.point(bid, vids[-1], vids[10], vids[20])
    for v in vids[:5]:
        await rig.change(bid, None, v, job=True)
    await rig.ret.run()
    check = await rig.db.read(lambda c: (
        c.execute("PRAGMA integrity_check").fetchone()[0], c.execute("PRAGMA foreign_key_check").fetchall()
    ))  # fmt: skip
    assert check[0] == "ok" and check[1] == []
    assert json.dumps(sorted(await rig.ids("version")))  # still readable
