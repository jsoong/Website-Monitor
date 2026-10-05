"""Backup and restore through the API and the CLI, with the engine really restarting into the
restored data."""

from __future__ import annotations

import asyncio
import random
import zipfile
from pathlib import Path

import httpx

from pagewatch.cli.main import main as cli_main
from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import EXIT_RESTART, Engine
from pagewatch.engine.paths import DataDir
from pagewatch.engine.store.db import latest_schema_version
from tests.support.m5 import add


async def names(engine: Engine) -> list[str]:
    rows = await engine.db.read(
        lambda c: c.execute("SELECT name FROM bookmark ORDER BY id").fetchall()
    )
    return [r[0] for r in rows]


async def stopped(engine: Engine, within: float = 5.0) -> bool:
    for _ in range(int(within / 0.05)):
        if engine._stopped.is_set():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_post_backup_writes_a_zip_in_the_backups_folder(
    client: httpx.AsyncClient, engine: Engine, data_dir: DataDir
) -> None:
    await add(client, "one")
    r = await client.post("/backup")
    assert r.status_code == 200, r.text
    body = r.json()
    path = Path(body["path"])
    assert path.parent == data_dir.backups_dir and body["size_bytes"] > 0
    assert body["include_blobs"] is False and body["schema_version"] == latest_schema_version()
    with zipfile.ZipFile(path) as z:
        assert "pagewatch.db" in z.namelist() and not any(
            n.startswith("blobs/") for n in z.namelist()
        )


async def test_a_backup_can_include_blobs_and_go_to_a_chosen_path(
    client: httpx.AsyncClient, engine: Engine, tmp_path: Path
) -> None:
    await add(client, "one")
    engine.blobs.put(b"some page " * 100)
    dest = tmp_path / "usb" / "moving.zip"
    r = await client.post("/backup", json={"include_blobs": True, "path": str(dest)})
    assert r.status_code == 200 and Path(r.json()["path"]) == dest and r.json()["include_blobs"]
    with zipfile.ZipFile(dest) as z:
        assert any(n.startswith("blobs/") for n in z.namelist())
    again = await client.post("/backup", json={"path": str(dest)})
    assert again.status_code == 409 and "already exists" in again.json()["detail"]


async def test_the_include_blobs_setting_is_the_default(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    engine.blobs.put(b"x" * 500)
    await client.put("/settings", json={"backup_include_blobs": True})
    assert (await client.post("/backup")).json()["include_blobs"] is True


async def test_a_restore_restarts_the_engine_which_comes_back_with_the_old_data(
    client: httpx.AsyncClient, engine: Engine, data_dir: DataDir, clock: FakeClock,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    await add(client, "kept")
    backup = (await client.post("/backup")).json()["path"]
    await add(client, "added later")
    assert await names(engine) == ["kept", "added later"]

    r = await client.post("/restore", json={"path": backup})
    assert r.status_code == 202 and r.json()["restart"] is True
    assert await names(engine) == ["kept", "added later"]  # nothing changes until it restarts
    assert await stopped(engine) and engine.exit_code == EXIT_RESTART
    await engine.stop()

    again = Engine(data_dir, clock=clock, worker_mode="thread", toast_backend=toasts,
                   settings_overrides={"startup_delay_s": 0.0}, rng=random.Random(1))  # fmt: skip
    await again.start()
    try:
        assert await names(again) == ["kept"]
        assert next(
            data_dir.backups_dir.glob("pre-restore-*.db")
        ).exists()  # the other state is kept
        assert not (data_dir.root / "restore" / "pending.zip").exists()
    finally:
        await again.stop()


async def test_a_bad_restore_is_refused_and_nothing_happens(
    client: httpx.AsyncClient, engine: Engine, tmp_path: Path
) -> None:
    await add(client, "live")
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    r = await client.post("/restore", json={"path": str(junk)})
    assert r.status_code == 422 and "not a backup zip" in r.json()["detail"]
    missing = await client.post("/restore", json={"path": str(tmp_path / "nope.zip")})
    assert missing.status_code == 422
    await asyncio.sleep(0.5)
    assert not engine._stopped.is_set() and await names(engine) == ["live"]
    assert not (engine.data_dir.root / "restore").exists()


async def test_a_backup_from_a_newer_build_is_refused(
    client: httpx.AsyncClient, engine: Engine, tmp_path: Path
) -> None:
    good = Path((await client.post("/backup")).json()["path"])
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(newer, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "manifest.json":
                data = data.replace(
                    f'"schema_version": {latest_schema_version()}'.encode(),
                    f'"schema_version": {latest_schema_version() + 1}'.encode(),
                )
            dst.writestr(info.filename, data)
    r = await client.post("/restore", json={"path": str(newer)})
    assert r.status_code == 422 and "newer PageWatch" in r.json()["detail"]


async def test_the_cli_backs_up_and_restores(
    client: httpx.AsyncClient, engine: Engine, data_dir: DataDir, tmp_path: Path,
    capsys: object,
) -> None:  # fmt: skip
    await add(client, "kept")

    def run(*args: str) -> int:
        return cli_main(["--data-dir", str(data_dir.root), *args])

    out = tmp_path / "from-cli.zip"
    assert await asyncio.to_thread(run, "backup", "--out", str(out), "--blobs") == 0
    assert out.exists()
    await add(client, "later")
    assert await asyncio.to_thread(run, "restore", str(out)) == 0
    assert await stopped(engine) and engine.exit_code == EXIT_RESTART
    assert await asyncio.to_thread(run, "restore", str(tmp_path / "missing.zip")) != 0


async def test_health_reports_the_last_backup_and_maintenance(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    h = (await client.get("/health")).json()
    assert (
        h["last_backup_at"] is None and h["rss_total_mb"] is None
    )  # (no unattended machinery here)
