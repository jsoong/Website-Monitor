"""Backup and restore: a consistent snapshot, nothing secret, and no way to wreck live data."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pagewatch.engine.paths import DataDir
from pagewatch.engine.store import backup
from pagewatch.engine.store.backup import BackupError
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.engine.store.db import Database, latest_schema_version, migrate

NOW = datetime(2026, 3, 9, 3, 0, 0, tzinfo=UTC)


class Rig:
    def __init__(self, data: DataDir, db: Database) -> None:
        self.data = data
        self.db = db
        self.blobs = BlobStore(data.blobs_dir)

    def add_bookmark(self, name: str) -> None:
        self.db.write_sync(
            lambda c: c.execute(
                "INSERT INTO bookmark(name,url,schedule_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (name, f"http://{name}.test/", "{}", "now", "now"),
            )  # fmt: skip
        )

    def names(self) -> list[str]:
        rows = self.db.read_sync(
            lambda c: c.execute("SELECT name FROM bookmark ORDER BY id").fetchall()
        )
        return [r[0] for r in rows]

    def make(self, **kw: object) -> backup.BackupInfo:
        return backup.create_backup(
            self.db,
            self.data,
            now=NOW,
            app_version="9.9",
            **{"include_blobs": False, **kw},  # type: ignore[arg-type]
        )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    data = DataDir(tmp_path / "data").ensure()
    migrate(data.db_path)
    db = Database(data.db_path)
    db.start()
    r = Rig(data, db)
    try:
        yield r
    finally:
        r.db.close()  # (a test that restored has swapped in a new database object)


def names_in(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


# -- creating ---------------------------------------------------------------------------------


async def test_a_backup_holds_the_snapshot_settings_macros_and_plugins_but_no_blobs(
    rig: Rig,
) -> None:
    rig.add_bookmark("alpha")
    rig.db.write_sync(lambda c: c.execute("INSERT INTO setting VALUES('keep_awake','true')"))
    rig.db.write_sync(lambda c: c.execute("INSERT INTO macro(name,steps_json) VALUES('m','[]')"))
    (rig.data.plugins_dir / "price.py").write_text("def after_check(ctx, run): pass\n")
    rig.blobs.put(b"page content " * 100)
    info = rig.make()
    assert info.path.parent == rig.data.backups_dir and info.path.name.startswith("backup-manual-")
    assert names_in(info.path) == {
        "manifest.json", "pagewatch.db", "settings.json", "macros.json", "plugins/price.py"
    }  # fmt: skip
    with zipfile.ZipFile(info.path) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["app_version"] == "9.9" and manifest["include_blobs"] is False
        assert manifest["schema_version"] == latest_schema_version()
        assert json.loads(z.read("settings.json")) == {"keep_awake": True}
        assert json.loads(z.read("macros.json"))[0]["name"] == "m"


async def test_blobs_are_included_only_when_asked(rig: Rig) -> None:
    digest = rig.blobs.put(b"a page " * 100)
    with_blobs = rig.make(include_blobs=True)
    assert f"blobs/{digest[:2]}/{digest[2:4]}/{digest}.zst" in names_in(with_blobs.path)
    assert with_blobs.include_blobs and backup.validate_backup(with_blobs.path).include_blobs


async def test_nothing_secret_is_in_a_backup(rig: Rig) -> None:
    (rig.data.root / "engine.lock").write_text('{"token": "TOPSECRET"}')
    (rig.data.logs_dir / "engine.log").write_text("Authorization: Bearer abcdefgh12345678")
    info = rig.make(include_blobs=True)
    with zipfile.ZipFile(info.path) as z:
        assert not any("lock" in n or "log" in n for n in z.namelist())
        assert all(b"TOPSECRET" not in z.read(n) for n in z.namelist())


async def test_the_snapshot_is_consistent_while_checks_keep_writing(rig: Rig) -> None:
    for i in range(50):
        rig.add_bookmark(f"b{i}")
    stop = threading.Event()

    def churn() -> None:
        n = 0
        while not stop.is_set():
            rig.add_bookmark(f"live{n}")
            n += 1

    t = threading.Thread(target=churn)
    t.start()
    try:
        info = rig.make()
    finally:
        stop.set()
        t.join()
    with zipfile.ZipFile(info.path) as z:
        z.extract("pagewatch.db", rig.data.root / "peek")
    conn = sqlite3.connect(rig.data.root / "peek" / "pagewatch.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0] >= 50
    finally:
        conn.close()


async def test_an_explicit_destination_is_used_and_never_overwritten(
    rig: Rig, tmp_path: Path
) -> None:
    dest = tmp_path / "elsewhere" / "mine.zip"
    info = rig.make(dest=dest)
    assert info.path == dest and dest.exists()
    with pytest.raises(BackupError, match="already exists"):
        rig.make(dest=dest)
    assert not list(dest.parent.glob("*.tmp")) and not list(dest.parent.glob(".pw-backup-*"))


async def test_two_backups_in_the_same_second_do_not_collide(rig: Rig) -> None:
    a, b = rig.make(), rig.make()
    assert a.path != b.path and a.path.exists() and b.path.exists()


async def test_only_automatic_backups_are_pruned_and_the_newest_are_kept(rig: Rig) -> None:
    for day in range(1, 21):
        stamp = (NOW + timedelta(days=day)).strftime("%Y%m%d-%H%M%S")
        (rig.data.backups_dir / f"backup-auto-{stamp}.zip").write_bytes(b"x")
    manual = rig.data.backups_dir / "backup-manual-20250101-000000.zip"
    manual.write_bytes(b"x")
    premigration = rig.data.backups_dir / "pre-migration-v1-20250101T000000Z.db"
    premigration.write_bytes(b"x")
    doomed = backup.prune_backups(rig.data.backups_dir, keep=14)
    left = sorted(p.name for p in rig.data.backups_dir.glob("backup-auto-*.zip"))
    assert len(doomed) == 6 and len(left) == 14
    assert left[0].startswith("backup-auto-20260316")  # the 14 newest of days 1..20 are 7..20
    assert manual.exists() and premigration.exists()


# -- validating -------------------------------------------------------------------------------


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def manifest(schema: int | None = None) -> bytes:
    return json.dumps({
        "created_at": "t", "app_version": "1", "include_blobs": False,
        "schema_version": schema if schema is not None else latest_schema_version(),
    }).encode()  # fmt: skip


async def test_a_valid_backup_validates(rig: Rig) -> None:
    m = backup.validate_backup(rig.make().path)
    assert m.schema_version == latest_schema_version() and m.app_version == "9.9"


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({"notes.txt": b"hello"}, "not a PageWatch backup"),
        ({"manifest.json": b"{not json", "pagewatch.db": b"x"}, "manifest is unreadable"),
        ({"manifest.json": manifest(), "pagewatch.db": b"this is not a database at all" * 50},
         "damaged"),
        ({"manifest.json": manifest(), "pagewatch.db": b"x", "../evil.txt": b"x"}, "unsafe path"),
        ({"manifest.json": manifest(), "pagewatch.db": b"x", "plugins/../../evil.py": b"x"},
         "unsafe path"),
        ({"manifest.json": manifest(), "pagewatch.db": b"x", "/etc/passwd": b"x"}, "unsafe path"),
        ({"manifest.json": manifest(), "pagewatch.db": b"x", "C:\\windows\\x": b"x"}, "unsafe path"),
        ({"manifest.json": manifest(), "pagewatch.db": b"x", "plugins\\..\\x.py": b"x"},
         "unsafe path"),
    ],
)  # fmt: skip
def test_bad_zips_are_refused_before_anything_is_touched(
    tmp_path: Path, members: dict[str, bytes], message: str
) -> None:
    with pytest.raises(BackupError, match=message):
        backup.validate_backup(make_zip(tmp_path / "bad.zip", members))


def test_an_absurdly_large_manifest_is_refused_without_being_read(tmp_path: Path) -> None:
    huge = make_zip(
        tmp_path / "huge.zip", {"manifest.json": b" " * (2 * 1024 * 1024), "pagewatch.db": b"x"}
    )
    with pytest.raises(BackupError, match="far larger"):
        backup.validate_backup(huge)


def test_not_a_zip_and_not_a_file_are_refused(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"PK this is not really a zip")
    with pytest.raises(BackupError, match="not a backup zip"):
        backup.validate_backup(junk)
    with pytest.raises(BackupError, match="not a file"):
        backup.validate_backup(tmp_path / "missing.zip")


async def test_a_backup_from_a_newer_build_is_refused_with_a_clear_reason(
    rig: Rig, tmp_path: Path
) -> None:
    good = rig.make().path
    with zipfile.ZipFile(good) as z:
        db_bytes = z.read("pagewatch.db")
    newer = make_zip(tmp_path / "newer.zip", {
        "manifest.json": manifest(latest_schema_version() + 1), "pagewatch.db": db_bytes,
    })  # fmt: skip
    with pytest.raises(BackupError, match="newer PageWatch"):
        backup.validate_backup(newer)


async def test_a_manifest_that_disagrees_with_its_database_is_refused(
    rig: Rig, tmp_path: Path
) -> None:
    with zipfile.ZipFile(rig.make().path) as z:
        db_bytes = z.read("pagewatch.db")
    liar = make_zip(tmp_path / "liar.zip", {"manifest.json": manifest(0), "pagewatch.db": db_bytes})
    with pytest.raises(BackupError, match="does not match"):
        backup.validate_backup(liar)


# -- restoring --------------------------------------------------------------------------------


async def test_a_restore_replaces_the_database_keeps_the_old_one_aside_and_brings_back_blobs(
    rig: Rig,
) -> None:
    rig.add_bookmark("kept")
    digest = rig.blobs.put(b"snapshot of the past " * 50)
    (rig.data.plugins_dir / "old.py").write_text("# from the backup\n")
    zip_path = rig.make(include_blobs=True).path

    rig.add_bookmark("added after the backup")
    rig.blobs.delete(digest)
    (rig.data.plugins_dir / "old.py").write_text("# edited since\n")
    assert rig.names() == ["kept", "added after the backup"]

    backup.stage_restore(rig.data, zip_path, NOW)
    assert backup.pending_restore(rig.data)
    assert rig.names() == ["kept", "added after the backup"]  # staging changes nothing live
    rig.db.close()

    assert backup.apply_pending_restore(rig.data, NOW + timedelta(minutes=1))
    assert not backup.pending_restore(rig.data)
    assert not (rig.data.root / "restore" / "pending.json").exists()
    migrate(rig.data.db_path)
    db = Database(rig.data.db_path)
    db.start()
    try:
        rows = db.read_sync(lambda c: [r[0] for r in c.execute("SELECT name FROM bookmark")])
        assert rows == ["kept"]
    finally:
        db.close()
    assert BlobStore(rig.data.blobs_dir).exists(digest)  # the content store came back too
    assert (rig.data.plugins_dir / "old.py").read_text() == "# from the backup\n"
    # what was replaced is still there, as a clean database
    aside = next(rig.data.backups_dir.glob("pre-restore-*.db"))
    old = sqlite3.connect(aside)
    try:
        assert [r[0] for r in old.execute("SELECT name FROM bookmark ORDER BY id")] == [
            "kept", "added after the backup",
        ]  # fmt: skip
    finally:
        old.close()
    rig.db = Database(rig.data.db_path)  # (the fixture closes whatever ``rig.db`` is)
    rig.db.start()


def leave_wal_files(db_path: Path) -> list[Path]:
    wal, shm = Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")
    wal.write_bytes(b"left over from the old database")
    shm.write_bytes(b"x")
    return [wal, shm]


async def test_stale_wal_files_do_not_survive_a_restore(rig: Rig) -> None:
    zip_path = rig.make().path
    backup.stage_restore(rig.data, zip_path, NOW)
    rig.db.close()
    leftovers = leave_wal_files(rig.data.db_path)
    assert backup.apply_pending_restore(rig.data, NOW)
    assert not any(p.exists() for p in leftovers)
    rig.db = Database(rig.data.db_path)
    rig.db.start()


async def test_a_staged_zip_that_went_bad_is_set_aside_and_live_data_is_untouched(rig: Rig) -> None:
    rig.add_bookmark("live")
    zip_path = rig.make().path
    backup.stage_restore(rig.data, zip_path, NOW)
    pending = rig.data.root / "restore" / "pending.zip"
    pending.write_bytes(pending.read_bytes()[:200])  # truncated on disk after staging
    rig.db.close()
    assert backup.apply_pending_restore(rig.data, NOW) is False
    assert not pending.exists() and list((rig.data.root / "restore").glob("failed-*.zip"))
    rig.db = Database(rig.data.db_path)
    rig.db.start()
    assert rig.names() == ["live"]


async def test_nothing_pending_is_a_no_op(rig: Rig) -> None:
    assert backup.apply_pending_restore(rig.data, NOW) is False


async def test_staging_a_bad_zip_changes_nothing(rig: Rig, tmp_path: Path) -> None:
    junk = make_zip(tmp_path / "x.zip", {"hello.txt": b"hi"})
    with pytest.raises(BackupError):
        backup.stage_restore(rig.data, junk, NOW)
    assert not backup.pending_restore(rig.data) and not (rig.data.root / "restore").exists()


async def test_a_damaged_current_database_is_kept_as_raw_files_when_restoring(rig: Rig) -> None:
    zip_path = rig.make().path
    backup.stage_restore(rig.data, zip_path, NOW)
    rig.db.close()
    rig.data.db_path.write_bytes(os.urandom(4096))  # the reason someone restores
    assert backup.apply_pending_restore(rig.data, NOW)
    raw = next(rig.data.backups_dir.glob("pre-restore-*-raw"))
    assert (raw / "pagewatch.db").stat().st_size == 4096
    rig.db = Database(rig.data.db_path)
    rig.db.start()
