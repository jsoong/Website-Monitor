"""Building blocks for the upgrade tests: run "a release" of the engine against a data folder,
and compare data folders table by table."""

from __future__ import annotations

import random
import shutil
import sqlite3
import zipfile
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from pagewatch.engine.store import db as dbmod
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

FAST: dict[str, Any] = {
    "startup_delay_s": 0.0,
    "per_host_min_gap_s": 0.0,
    "per_host_concurrency": 16,
    "worker_processes": 2,
    "toast_coalesce_s": 0.0,
}

#: A data folder written by the first release, kept in git so that every later migration is tried
#: on data a real user has: see tools/gen_upgrade_fixture.py.
RELEASE_1_FOLDER = (
    Path(__file__).resolve().parents[1] / "fixtures" / "upgrade" / "release-0.1.0.zip"
)


class Release:
    """One running release of the engine on a data folder: the engine, its API and a clock."""

    def __init__(self, engine: Engine, client: httpx.AsyncClient, clock: FakeClock) -> None:
        self.engine, self.client, self.clock = engine, client, clock

    async def settle(self) -> None:
        await settle(self.engine, self.clock)

    async def advance(self, seconds: float) -> None:
        await advance(self.engine, self.clock, seconds)


@asynccontextmanager
async def running(dd: DataDir, *, seed: int = 7) -> Any:
    """Start the engine on ``dd`` (whatever migrations are in force), yield it, stop it."""
    clock = FakeClock()
    engine = Engine(
        dd,
        clock=clock,
        worker_mode="thread",
        settings_overrides=FAST,
        toast_backend=LogToastBackend(),
        rng=random.Random(seed),
    )
    await engine.start()
    server = ApiServer(engine)
    port = await server.start()
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {engine.token}"},
        timeout=20,
    )
    try:
        yield Release(engine, client, clock)
    finally:
        await client.aclose()
        await server.stop()
        await engine.stop()


async def populate(release: Release, site: FixtureSite) -> list[int]:
    """Three bookmarks; the first changes once (two versions, one change); the second is read."""
    ids: list[int] = []
    for i in range(3):
        site.set(f"/p{i}", article(f"story {i} alpha", title=f"Upgrade {i}"))
        r = await release.client.post(
            "/bookmarks",
            json={
                "url": site.url(f"/p{i}"),
                "name": f"Upgrade {i}",
                "schedule": {"interval_s": 60},
            },
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    await release.settle()
    site.set("/p0", article("story 0 alpha", "story 0 beta, new this time", title="Upgrade 0"))
    await release.advance(90)
    assert (await release.client.post(f"/bookmarks/{ids[1]}/read")).status_code == 200
    changes = (await release.client.get(f"/bookmarks/{ids[0]}/changes")).json()["items"]
    assert len(changes) == 1, "the fixture data must contain a change, or the tests prove nothing"
    return ids


# -- comparing data -----------------------------------------------------------------------


def dump(
    db_path: Path, *, skip: tuple[str, ...] = ("schema_version",)
) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, for comparing a database before and after."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        return {
            t: [tuple(row) for row in conn.execute(f'SELECT * FROM "{t}"')]
            for t in tables
            if t not in skip
        }
    finally:
        conn.close()


def schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    finally:
        conn.close()


def columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()


def blob_listing(dd: DataDir) -> dict[str, int]:
    return {
        str(p.relative_to(dd.blobs_dir)): p.stat().st_size
        for p in sorted(dd.blobs_dir.rglob("*"))
        if p.is_file()
    }


def pre_migration_backups(dd: DataDir) -> list[Path]:
    return sorted(dd.backups_dir.glob("pre-migration-v*.db"))


# -- a hypothetical next release ----------------------------------------------------------


def latest() -> int:
    return dbmod.latest_schema_version()


@contextmanager
def releases_with(*extra_sql: str) -> Iterator[int]:
    """Pretend the build has ``extra_sql`` as the migrations after the real latest one.

    Yields the schema version that results. Numbering from the real latest keeps the tests valid
    as milestones add real migrations.
    """
    real = dbmod._migration_files()
    base = real[-1][0]
    fake = [(base + 1 + i, sql) for i, sql in enumerate(extra_sql)]
    original = dbmod._migration_files
    dbmod._migration_files = lambda: [*real, *fake]  # type: ignore[assignment]
    try:
        yield base + len(fake)
    finally:
        dbmod._migration_files = original  # type: ignore[assignment]


def unpack_release_1(into: Path) -> DataDir:
    """The frozen data folder of the first release, unpacked at ``into``."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    with zipfile.ZipFile(RELEASE_1_FOLDER) as zf:
        zf.extractall(into)
    dd = DataDir(into)
    dd.db_path.write_bytes((into / "pagewatch.sqlite").read_bytes())
    (into / "pagewatch.sqlite").unlink()
    return dd.ensure()
