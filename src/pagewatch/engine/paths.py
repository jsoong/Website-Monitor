"""Data folder layout. Everything PageWatch knows lives under one folder."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataDir:
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "pagewatch.db"

    @property
    def blobs_dir(self) -> Path:
        return self.root / "blobs"

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def lock_path(self) -> Path:
        return self.root / "engine.lock"

    @property
    def mutex_path(self) -> Path:
        return self.root / "engine.mutex"

    def ensure(self) -> DataDir:
        for d in (
            self.root,
            self.blobs_dir,
            self.profiles_dir,
            self.logs_dir,
            self.backups_dir,
            self.plugins_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self


def default_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PageWatch"
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "pagewatch"


def resolve_data_dir(arg: str | os.PathLike[str] | None) -> DataDir:
    """``--data-dir`` wins, then ``PAGEWATCH_DATA_DIR``, then the platform default."""
    chosen = arg or os.environ.get("PAGEWATCH_DATA_DIR")
    root = Path(chosen) if chosen else default_data_root()
    return DataDir(root.expanduser().resolve())
