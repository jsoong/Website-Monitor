"""M8 acceptance, as far as it can run here: *survives an upgrade with data intact*.

"Install the new build over the old one. Migrations run at startup after an automatic backup"
(SPEC, Updates). What these tests do, in order of what a user would suffer if it broke:

* the upgrade keeps every row and blob, and the backup is exactly the pre-upgrade state;
* a migration that fails halfway leaves the database as it was;
* an older build refuses to open newer data (and leaves it alone);
* skipping releases works; restarting does not pile up backups; a first install takes none.

The next release is hypothetical: a migration numbered after the real latest one, so the tests
stay true as later milestones add real migrations.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pagewatch.engine.paths import DataDir
from pagewatch.engine.store import db as dbmod
from tests.packaging.upgrade_support import (
    RELEASE_1_FOLDER,
    blob_listing,
    columns,
    dump,
    latest,
    populate,
    pre_migration_backups,
    releases_with,
    running,
    schema_version,
    unpack_release_1,
)
from tests.support.fixture_site import FixtureSite

ADD_TABLE_AND_COLUMN = (
    "ALTER TABLE bookmark ADD COLUMN upgrade_marker TEXT;"
    "CREATE TABLE upgrade_feature (id INTEGER PRIMARY KEY, bookmark_id INTEGER, note TEXT);"
)


async def release_n_data(tmp_path: Path, site: FixtureSite) -> tuple[DataDir, list[int]]:
    """A data folder that the build as it is today has been using."""
    dd = DataDir(tmp_path / "data").ensure()
    async with running(dd) as release:
        ids = await populate(release, site)
    return dd, ids


# -- the upgrade ----------------------------------------------------------------------------


async def test_the_upgrade_keeps_every_row_and_blob_and_the_backup_is_the_old_state(
    tmp_path: Path, site: FixtureSite
) -> None:
    dd, ids = await release_n_data(tmp_path, site)
    before, blobs_before, was = dump(dd.db_path), blob_listing(dd), latest()
    assert before["bookmark"] and before["version"] and before["change"], "real data to keep"
    assert not pre_migration_backups(dd), "no backup while there is nothing to migrate"

    with releases_with(ADD_TABLE_AND_COLUMN) as now:  # the new build is installed over the old
        assert dbmod.migrate(dd.db_path, dd.backups_dir) == now
        after = dump(dd.db_path)

        # every old table keeps every old row, apart from the column the migration added
        assert set(before) <= set(after) and "upgrade_feature" in after
        assert after["upgrade_feature"] == []
        index = columns(dd.db_path, "bookmark").index("upgrade_marker")
        for table, rows in before.items():
            if table == "bookmark":
                assert [r[:index] + r[index + 1 :] for r in after[table]] == rows
                assert {r[index] for r in after[table]} == {None}
            else:
                assert after[table] == rows, f"{table} changed in the upgrade"
        assert schema_version(dd.db_path) == now
        assert blob_listing(dd) == blobs_before  # migrations never touch page content

        # the backup is a usable database holding exactly what the user had before the upgrade
        (backup,) = pre_migration_backups(dd)
        assert backup.name.startswith(f"pre-migration-v{was}-")
        assert schema_version(backup) == was
        assert dump(backup) == before
        assert "upgrade_marker" not in columns(backup, "bookmark")

        # and the new release runs on it: same bookmarks, readable history, checks still work
        async with running(dd) as release:
            listed = (await release.client.get("/bookmarks")).json()["items"]
            assert {b["id"]: b["name"] for b in listed} == {i: f"Upgrade {i - 1}" for i in ids}
            diff = await release.client.get(f"/bookmarks/{ids[0]}/diff?view=text")
            assert diff.status_code == 200  # the blobs are still readable through the new schema
            assert (await release.client.get("/health")).json()["bookmarks"] == 3
            site.set("/p2", site_page("story 2 alpha", "something new after the upgrade"))
            # a restarted FakeClock starts at its epoch again, before the stored due times
            await release.advance(300)
            changes = (await release.client.get(f"/bookmarks/{ids[2]}/changes")).json()["items"]
            assert len(changes) == 1


def site_page(*items: str) -> str:
    from tests.support.fixture_site import article

    return article(*items, title="Upgrade 2")


async def test_the_engine_itself_takes_the_backup_when_it_starts_on_old_data(
    tmp_path: Path, site: FixtureSite
) -> None:
    """Not just ``migrate()``: a real start-up of the new release is what upgrades the data."""
    dd, _ = await release_n_data(tmp_path, site)
    was = latest()
    with releases_with(ADD_TABLE_AND_COLUMN) as now:
        async with running(dd) as release:
            assert (await release.client.get("/health")).status_code == 200
        assert schema_version(dd.db_path) == now
        (backup,) = pre_migration_backups(dd)
        assert schema_version(backup) == was


async def test_several_missed_releases_upgrade_in_one_step_with_one_backup(
    tmp_path: Path, site: FixtureSite
) -> None:
    dd, _ = await release_n_data(tmp_path, site)
    before, was = dump(dd.db_path), latest()
    second = "CREATE TABLE feature_two (id INTEGER PRIMARY KEY, note TEXT);"
    third = "INSERT INTO feature_two (note) SELECT 'row ' || id FROM bookmark;"  # needs the second
    with releases_with(ADD_TABLE_AND_COLUMN, second, third) as now:
        assert now == was + 3
        assert dbmod.migrate(dd.db_path, dd.backups_dir) == now
        assert len(dump(dd.db_path)["feature_two"]) == len(before["bookmark"])
        (backup,) = pre_migration_backups(dd)
        assert backup.name.startswith(f"pre-migration-v{was}-") and dump(backup) == before


# -- when it goes wrong ------------------------------------------------------------------------


async def test_a_migration_that_fails_halfway_leaves_the_database_as_it_was(
    tmp_path: Path, site: FixtureSite
) -> None:
    dd, _ = await release_n_data(tmp_path, site)
    before, was = dump(dd.db_path), latest()
    broken = "ALTER TABLE bookmark ADD COLUMN half_done TEXT; THIS IS NOT SQL;"
    with releases_with(broken), pytest.raises(sqlite3.Error):
        dbmod.migrate(dd.db_path, dd.backups_dir)

    assert schema_version(dd.db_path) == was
    assert "half_done" not in columns(dd.db_path, "bookmark"), "the first statement was rolled back"
    assert dump(dd.db_path) == before
    (backup,) = pre_migration_backups(dd)  # taken before the attempt, still there
    assert dump(backup) == before

    # the corrected release then upgrades the same folder
    with releases_with("ALTER TABLE bookmark ADD COLUMN half_done TEXT;") as now:
        assert dbmod.migrate(dd.db_path, dd.backups_dir) == now
        assert "half_done" in columns(dd.db_path, "bookmark")


async def test_an_older_build_refuses_newer_data_and_leaves_it_alone(
    tmp_path: Path, site: FixtureSite
) -> None:
    """Downgrading the program does not downgrade the data folder, so it must not open it."""
    dd, _ = await release_n_data(tmp_path, site)
    with releases_with(ADD_TABLE_AND_COLUMN) as newer:
        dbmod.migrate(dd.db_path, dd.backups_dir)
    upgraded = dump(dd.db_path)

    # the build as it is now is "older" than the data (it only knows `latest()` migrations)
    with pytest.raises(dbmod.SchemaTooNew, match="install a newer PageWatch"):
        dbmod.migrate(dd.db_path, dd.backups_dir)
    assert schema_version(dd.db_path) == newer and dump(dd.db_path) == upgraded

    # and the same through a real engine process: a clear message, exit code 1, no lockfile
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "pagewatch.engine.main", "--data-dir", str(dd.root),
         "--workers", "thread", "--no-tray", "--no-console-log"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert done.returncode == 1
    assert "newer than this build" in done.stderr and "install a newer PageWatch" in done.stderr
    assert not dd.lock_path.exists()
    assert dump(dd.db_path) == upgraded


# -- backups do not pile up --------------------------------------------------------------------


async def test_restarting_an_up_to_date_install_never_adds_backups(
    tmp_path: Path, site: FixtureSite
) -> None:
    """The engine starts at every logon: backups are for migrations, not for restarts."""
    dd, _ = await release_n_data(tmp_path, site)
    for _ in range(3):
        async with running(dd):
            pass
    assert not pre_migration_backups(dd)


def test_a_first_install_creates_the_schema_and_takes_no_backup(tmp_path: Path) -> None:
    dd = DataDir(tmp_path / "fresh").ensure()
    assert dbmod.migrate(dd.db_path, dd.backups_dir) == latest()
    assert not pre_migration_backups(dd) and not list(dd.backups_dir.iterdir())


# -- real data from the first release ------------------------------------------------------------


def test_the_frozen_first_release_folder_is_still_there() -> None:
    assert RELEASE_1_FOLDER.is_file(), "tests/fixtures/upgrade/release-0.1.0.zip must stay in git"


async def test_data_written_by_the_first_release_opens_and_upgrades_to_whatever_is_latest(
    tmp_path: Path,
) -> None:
    """The check that matters most once real migrations exist: today's code on release 0.1.0's
    data. Until a second migration ships it proves 'opens, nothing pending, no backup'; after, it
    proves the real upgrade (backup of v1, data kept, new schema)."""
    dd = unpack_release_1(tmp_path / "data")
    original = tmp_path / "original.db"
    shutil.copy(dd.db_path, original)
    before = dump(original)
    assert schema_version(dd.db_path) == 1 and before["bookmark"] and before["change"]

    async with running(dd) as release:
        listed = (await release.client.get("/bookmarks")).json()["items"]
        assert [b["name"] for b in listed] == ["Upgrade 0", "Upgrade 1", "Upgrade 2"]
        for b in listed:
            runs = (await release.client.get(f"/bookmarks/{b['id']}/runs")).json()["items"]
            assert runs, "the history survived"
        diff = await release.client.get(f"/bookmarks/{listed[0]['id']}/diff?view=text")
        assert diff.status_code == 200

    assert schema_version(dd.db_path) == latest()
    backups = pre_migration_backups(dd)
    if latest() == 1:
        assert not backups
    else:
        (backup,) = backups
        assert backup.name.startswith("pre-migration-v1-")
        assert dump(backup) == before  # the old data, exactly as the first release left it
        for table, rows in before.items():  # nothing the user had has gone missing
            assert len(dump(dd.db_path)[table]) >= len(rows), table
