"""One engine per data folder, and discovery of the running engine.

``InstanceGuard`` is the per-data-folder mutex: a named kernel mutex on Windows, an
``flock`` on a lock file elsewhere (the development platform). ``engine.lock`` carries
``{pid, port, token, version, started_at}`` for the UI and CLI.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import psutil

from pagewatch.engine.paths import DataDir
from pagewatch.models import LockInfo

ERROR_ALREADY_EXISTS = 183


class InstanceGuard:
    def __init__(self, data_dir: DataDir) -> None:
        self._data_dir = data_dir
        self._handle: Any = None
        self._fd: int | None = None

    @property
    def mutex_name(self) -> str:
        digest = hashlib.sha1(str(self._data_dir.root).lower().encode()).hexdigest()[:16]
        return f"Local\\PageWatch-{digest}"

    def acquire(self) -> bool:
        """True if this process now owns the data folder, False if another engine does."""
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:  # pragma: no cover - exercised on Windows only
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _acquire_posix(self) -> bool:
        if sys.platform == "win32":
            return False
        import fcntl

        self._data_dir.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._data_dir.mutex_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if sys.platform == "win32":  # pragma: no cover
            if self._handle:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
                self._handle = None
            return
        if self._fd is not None:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# -- lockfile ---------------------------------------------------------------------------


def write_lockfile(data_dir: DataDir, info: LockInfo) -> None:
    """Atomic write; readable only by the current user where the OS supports it."""
    path = data_dir.lock_path
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(info.model_dump_json())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_lockfile(data_dir: DataDir, *, require_alive: bool = True) -> LockInfo | None:
    try:
        raw = json.loads(Path(data_dir.lock_path).read_text(encoding="utf-8"))
        info = LockInfo.model_validate(raw)
    except (OSError, ValueError):
        return None
    if require_alive and not psutil.pid_exists(info.pid):
        return None
    return info


def remove_lockfile(data_dir: DataDir, *, only_pid: int | None = None) -> None:
    if only_pid is not None:
        info = read_lockfile(data_dir, require_alive=False)
        if info is not None and info.pid != only_pid:
            return
    with contextlib.suppress(OSError):
        data_dir.lock_path.unlink()
