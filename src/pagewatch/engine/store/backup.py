"""Backup and restore (spec: Import, export, reports and backup).

A backup is one zip in ``backups\\``:

    manifest.json   format, created_at, app version, schema version, whether blobs are included
    pagewatch.db    an SQLite *online* backup: a consistent snapshot while checks keep running
                    (it holds the settings and the macros as well)
    settings.json   the settings, and macros.json the macros, as readable JSON
    plugins/...     the plugins folder
    blobs/...       the content store, only when asked for (off by default; needed to move PCs)

Secrets are never in it: the database holds key names only, and neither the lockfile nor the
logs are included.

Restoring never touches live data in the running engine. ``stage_restore`` validates the zip and
parks it; ``apply_pending_restore`` runs at the next start-up, before the database is opened,
copies the current database to ``backups\\pre-restore-*`` first, and only then swaps. The last
step before the swap is the only one that cannot be undone, and it is a rename.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pagewatch.engine.clock import iso
from pagewatch.engine.logs import get_logger
from pagewatch.engine.paths import DataDir
from pagewatch.engine.store.db import (
    Database,
    backup_database,
    connect,
    current_schema_version,
    latest_schema_version,
)

log = get_logger("pagewatch.backup")

FORMAT = 1
MAX_MANIFEST_BYTES = 1024 * 1024  # a real one is a few hundred bytes
KEEP_DEFAULT = 14
AUTO_PREFIX = "backup-auto-"
MANUAL_PREFIX = "backup-manual-"
_BLOB_NAME = re.compile(r"^blobs/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.zst$")
_STAMP = "%Y%m%d-%H%M%S"

Kind = Literal["auto", "manual"]


class BackupError(ValueError):
    """The backup could not be made, or the zip cannot be restored (the message says why)."""


@dataclass(slots=True)
class BackupInfo:
    path: Path
    size_bytes: int
    created_at: str
    include_blobs: bool
    schema_version: int


@dataclass(slots=True)
class Manifest:
    created_at: str
    app_version: str
    schema_version: int
    include_blobs: bool


# -- creating ---------------------------------------------------------------------------------


def _unique(path: Path) -> Path:
    n = 1
    candidate = path
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


def default_destination(data: DataDir, kind: Kind, now: datetime) -> Path:
    prefix = AUTO_PREFIX if kind == "auto" else MANUAL_PREFIX
    return _unique(data.backups_dir / f"{prefix}{now.strftime(_STAMP)}.zip")


def create_backup(
    db: Database,
    data: DataDir,
    *,
    include_blobs: bool,
    now: datetime,
    app_version: str,
    dest: Path | None = None,
    kind: Kind = "manual",
) -> BackupInfo:
    """Blocking (it copies the database and, optionally, every blob): call it in a thread."""
    target = dest if dest is not None else default_destination(data, kind, now)
    if target.exists():
        raise BackupError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = latest_schema_version()
    tmp_zip = target.with_name(target.name + ".tmp")
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".pw-backup-") as scratch:
        snapshot = Path(scratch) / "pagewatch.db"
        db.backup_sync(snapshot)
        snap = sqlite3.connect(snapshot)
        try:
            schema = current_schema_version(snap)
            settings = {
                k: json.loads(v) for k, v in snap.execute("SELECT key, value_json FROM setting")
            }
            macros = [
                {"id": i, "name": n, "steps": json.loads(s), "login_signal": ls and json.loads(ls)}
                for i, n, s, ls in snap.execute(
                    "SELECT id, name, steps_json, login_signal_json FROM macro"
                )
            ]
        finally:
            snap.close()
        manifest = Manifest(iso(now), app_version, schema, include_blobs)
        try:
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.writestr("manifest.json", json.dumps(asdict(manifest), indent=1))
                z.write(snapshot, "pagewatch.db")
                z.writestr("settings.json", json.dumps(settings, indent=1, sort_keys=True))
                z.writestr("macros.json", json.dumps(macros, indent=1))
                if data.plugins_dir.is_dir():
                    for path in sorted(data.plugins_dir.rglob("*")):
                        if path.is_file() and "__pycache__" not in path.parts:
                            z.write(
                                path, f"plugins/{path.relative_to(data.plugins_dir).as_posix()}"
                            )
                if include_blobs and data.blobs_dir.is_dir():
                    for path in sorted(data.blobs_dir.glob("??/??/*.zst")):
                        # already zstd-compressed: storing beats deflating it a second time
                        member = f"blobs/{path.parent.parent.name}/{path.parent.name}/{path.name}"
                        z.write(path, member, compress_type=zipfile.ZIP_STORED)
            with open(tmp_zip, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp_zip, target)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_zip.unlink()
            raise
    info = BackupInfo(target, target.stat().st_size, manifest.created_at, include_blobs, schema)
    log.info("backup_created", path=str(target), bytes=info.size_bytes, blobs=include_blobs)
    return info


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    """Keep the newest ``keep`` automatic backups. Manual, pre-migration and pre-restore ones are
    never removed automatically."""
    autos = sorted(backups_dir.glob(f"{AUTO_PREFIX}*.zip"))  # the stamp sorts by time
    doomed = autos[: max(0, len(autos) - keep)]
    for path in doomed:
        with contextlib.suppress(OSError):
            path.unlink()
    return doomed


# -- validating -------------------------------------------------------------------------------


def _safe_member(name: str) -> bool:
    if name.startswith(("/", "\\")) or ":" in name or "\\" in name:
        return False
    return ".." not in Path(name).parts


def validate_backup(path: Path) -> Manifest:
    """Everything that can be checked without extracting into live data. Raises ``BackupError``."""
    if not path.is_file():
        raise BackupError(f"{path} is not a file")
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"{path.name} is not a backup zip ({exc})") from exc
    with z:
        names = z.namelist()
        for name in names:
            if not _safe_member(name):
                raise BackupError(f"the zip contains an unsafe path: {name!r}")
        if "manifest.json" not in names or "pagewatch.db" not in names:
            raise BackupError("this is not a PageWatch backup (no manifest or database)")
        if z.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
            raise BackupError("the backup's manifest is far larger than any real one")
        try:
            raw = json.loads(z.read("manifest.json"))
            manifest = Manifest(
                str(raw["created_at"]), str(raw["app_version"]),
                int(raw["schema_version"]), bool(raw["include_blobs"]),
            )  # fmt: skip
        except (KeyError, ValueError, TypeError) as exc:
            raise BackupError(f"the backup's manifest is unreadable ({exc})") from exc
        if manifest.schema_version > latest_schema_version():
            raise BackupError(
                f"the backup is from a newer PageWatch (database v{manifest.schema_version}, "
                f"this build understands v{latest_schema_version()}); install a newer build"
            )
        with tempfile.TemporaryDirectory(prefix="pw-validate-") as scratch:
            probe = Path(scratch) / "pagewatch.db"
            with z.open("pagewatch.db") as src, open(probe, "wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                conn = sqlite3.connect(f"file:{probe}?mode=ro", uri=True)
                try:
                    verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    stored = current_schema_version(conn)
                finally:
                    conn.close()
            except sqlite3.DatabaseError as exc:
                raise BackupError(f"the backup's database is damaged ({exc})") from exc
        if verdict != "ok":
            raise BackupError(f"the backup's database failed its integrity check ({verdict})")
        if stored != manifest.schema_version:
            raise BackupError("the backup's manifest does not match its database")
    return manifest


# -- restoring --------------------------------------------------------------------------------


def _pending(data: DataDir) -> tuple[Path, Path]:
    folder = data.root / "restore"
    return folder / "pending.zip", folder / "pending.json"


def stage_restore(data: DataDir, source: Path, now: datetime) -> Manifest:
    """Validate ``source`` and park it for the next start-up. Changes no live data."""
    manifest = validate_backup(source)
    zip_path, marker = _pending(data)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    shutil.copyfile(source, tmp)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, zip_path)
    marker.write_text(json.dumps({"staged_at": iso(now), "source": str(source)}))
    log.info("restore_staged", source=str(source), created=manifest.created_at)
    return manifest


def pending_restore(data: DataDir) -> bool:
    return _pending(data)[0].is_file()


def _extract(z: zipfile.ZipFile, name: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".restoring")
    with z.open(name) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, target)


def apply_pending_restore(data: DataDir, now: datetime) -> bool:
    """Called at start-up, before anything opens the database. Returns True if a restore was
    applied. A restore that fails leaves the current data as it was and is set aside."""
    zip_path, marker = _pending(data)
    if not zip_path.is_file():
        return False
    stamp = now.strftime(_STAMP)
    try:
        validate_backup(zip_path)  # again: the file sat on disk since it was staged
        with tempfile.TemporaryDirectory(dir=data.root, prefix=".pw-restore-") as scratch:
            new_db = Path(scratch) / "pagewatch.db"
            with zipfile.ZipFile(zip_path) as z:
                _extract(z, "pagewatch.db", new_db)
                data.backups_dir.mkdir(parents=True, exist_ok=True)
                _set_current_aside(data, stamp)
                for name in z.namelist():  # nothing below is allowed to escape its folder
                    if _BLOB_NAME.match(name):
                        target = data.root / name
                        if (
                            not target.exists()
                        ):  # content-addressed: the same name is the same bytes
                            _extract(z, name, target)
                    elif name.startswith("plugins/") and not name.endswith("/"):
                        target = data.root / name
                        if data.plugins_dir.resolve() in target.resolve().parents:
                            _extract(z, name, target)
            for leftover in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(data.db_path) + leftover).unlink()
            os.replace(new_db, data.db_path)  # the point of no return
    except Exception as exc:
        log.error("restore_failed", error=str(exc), exc_info=True)
        failed = zip_path.with_name(f"failed-{stamp}.zip")
        with contextlib.suppress(OSError):
            os.replace(zip_path, failed)
        with contextlib.suppress(OSError):
            marker.unlink()
        return False
    with contextlib.suppress(OSError):
        zip_path.unlink()
        marker.unlink()
    log.info("restore_applied")
    return True


def _set_current_aside(data: DataDir, stamp: str) -> None:
    """Keep what is about to be replaced: a clean copy through SQLite's backup API, or, if the
    database is too damaged for that (the reason someone restores), the raw files."""
    if not data.db_path.exists():
        return
    safe = data.backups_dir / f"pre-restore-{stamp}.db"
    try:
        src = connect(data.db_path, readonly=True)
        try:
            backup_database(src, safe)
        finally:
            src.close()
    except sqlite3.Error:
        raw = data.backups_dir / f"pre-restore-{stamp}-raw"
        raw.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            part = Path(str(data.db_path) + suffix)
            if part.exists():
                shutil.copy2(part, raw / part.name)
