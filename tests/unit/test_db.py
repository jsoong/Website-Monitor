import asyncio
import sqlite3
from pathlib import Path

import pytest

from pagewatch.engine.store import db as dbmod
from pagewatch.engine.store.db import Database, SchemaTooNew, latest_schema_version, migrate

TABLES = {
    "folder", "bookmark", "version", "change", "view_diff_cache", "check_run", "macro",
    "action_job", "metric", "setting", "schema_version",
}  # fmt: skip


def table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_fresh_database_gets_the_full_schema(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    assert migrate(path, tmp_path / "bk") == latest_schema_version() >= 1
    assert table_names(path) >= TABLES
    assert not (tmp_path / "bk").exists()  # nothing to back up on a fresh database


def test_migrating_twice_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    migrate(path)
    assert migrate(path) == latest_schema_version()


def test_upgrade_backs_up_first_and_applies_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.db"
    migrate(path)
    real = dbmod._migration_files()
    n = real[-1][0]
    monkeypatch.setattr(
        dbmod,
        "_migration_files",
        lambda: [*real, (n + 1, "CREATE TABLE extra(x); INSERT INTO extra VALUES (1);")],
    )
    assert migrate(path, tmp_path / "bk") == n + 1
    assert list((tmp_path / "bk").glob(f"pre-migration-v{n}-*.db"))
    assert "extra" in table_names(path)


def test_failed_migration_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.db"
    migrate(path)
    real = dbmod._migration_files()
    n = real[-1][0]
    bad = "CREATE TABLE half_done(x); INSERT INTO no_such_table VALUES (1);"
    monkeypatch.setattr(dbmod, "_migration_files", lambda: [*real, (n + 1, bad)])
    with pytest.raises(sqlite3.Error):
        migrate(path, tmp_path / "bk")
    assert "half_done" not in table_names(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == n
    conn.close()


def test_database_from_the_future_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaTooNew):
        migrate(path)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    path = tmp_path / "t.db"
    migrate(path)
    d = Database(path)
    d.start()
    yield d  # type: ignore[misc]
    d.close()


def _add_bookmark(conn: sqlite3.Connection, name: str = "b") -> int:
    cur = conn.execute(
        "INSERT INTO bookmark(name,url,schedule_json,created_at,updated_at) VALUES(?,?,?,?,?)",
        (name, "https://example.com", "{}", "t", "t"),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


async def test_writer_commits_and_readers_see_it(db: Database) -> None:
    bid = await db.write(_add_bookmark)
    rows = await db.read(lambda c: [r["name"] for r in c.execute("SELECT name FROM bookmark")])
    assert rows == ["b"] and bid == 1
    pragma = await db.read(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
    assert pragma == "wal"


async def test_failed_write_rolls_back_everything(db: Database) -> None:
    def boom(conn: sqlite3.Connection) -> None:
        _add_bookmark(conn, "doomed")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await db.write(boom)
    assert await db.read(lambda c: c.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0]) == 0
    await db.write(_add_bookmark)  # the writer thread survived


async def test_readers_do_not_block_on_the_writer_and_writes_serialize(db: Database) -> None:
    await asyncio.gather(*[db.write(lambda c, i=i: _add_bookmark(c, f"b{i}")) for i in range(50)])
    names = await asyncio.gather(
        *[
            db.read(lambda c: c.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0])
            for _ in range(10)
        ]
    )
    assert set(names) == {50}


async def test_foreign_keys_on_and_bookmark_delete_cascades_over_versions_and_changes(
    db: Database,
) -> None:
    def build(conn: sqlite3.Connection) -> int:
        bid = _add_bookmark(conn)
        v1 = conn.execute(
            "INSERT INTO version(bookmark_id,fetched_at,blocks_hash,filtered_hash) VALUES(?,?,?,?)",
            (bid, "t", "a", "b"),
        ).lastrowid
        v2 = conn.execute(
            "INSERT INTO version(bookmark_id,fetched_at,blocks_hash,filtered_hash) VALUES(?,?,?,?)",
            (bid, "t", "c", "d"),
        ).lastrowid
        cid = conn.execute(
            "INSERT INTO change(bookmark_id,old_version_id,new_version_id,detected_at) VALUES(?,?,?,?)",
            (bid, v1, v2, "t"),
        ).lastrowid
        conn.execute(
            "INSERT INTO action_job(change_id,action_index,action_type,status) VALUES(?,?,?,?)",
            (cid, 0, "toast", "queued"),
        )
        return bid

    bid = await db.write(build)
    with pytest.raises(sqlite3.IntegrityError):  # foreign keys are enforced
        await db.write(
            lambda c: c.execute(
                "INSERT INTO version(bookmark_id,fetched_at,blocks_hash,filtered_hash) "
                "VALUES(999,'t','a','b')"
            )
        )
    await db.write(lambda c: c.execute("DELETE FROM bookmark WHERE id=?", (bid,)))
    for table in ("version", "change", "action_job"):
        n = await db.read(lambda c, t=table: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        assert n == 0, table


async def test_online_backup_is_a_usable_database(db: Database, tmp_path: Path) -> None:
    await db.write(_add_bookmark)
    dest = tmp_path / "copy.db"
    await asyncio.to_thread(db.backup_sync, dest)
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0] == 1
    conn.close()


async def test_a_backup_leaves_the_reader_pool_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Soak finding: a backup through a reader left a full copy of the database in that reader's
    page cache, one reader per night. It runs on a connection of its own, closed afterwards, so
    it must never go through the reader pool."""
    path = tmp_path / "pagewatch.db"
    migrate(path)
    db = Database(path)
    db.start()

    def through_a_reader(fn: object) -> None:
        raise AssertionError("the backup used a pooled reader connection")

    monkeypatch.setattr(db, "_run_read", through_a_reader)
    try:
        await asyncio.to_thread(db.backup_sync, tmp_path / "copy.db")
        copy = sqlite3.connect(tmp_path / "copy.db")
        try:
            assert copy.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 1
        finally:
            copy.close()
    finally:
        db.close()
