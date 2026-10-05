import os

from pagewatch.engine.instance import (
    InstanceGuard,
    read_lockfile,
    remove_lockfile,
    write_lockfile,
)
from pagewatch.engine.paths import DataDir
from pagewatch.models import LockInfo


def test_second_guard_on_same_folder_is_refused_until_release(data_dir: DataDir) -> None:
    first, second = InstanceGuard(data_dir), InstanceGuard(data_dir)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_different_folders_do_not_conflict(data_dir: DataDir, tmp_path_factory: object) -> None:
    other = DataDir(data_dir.root.parent / "other").ensure()
    a, b = InstanceGuard(data_dir), InstanceGuard(other)
    assert a.acquire() and b.acquire()
    assert a.mutex_name != b.mutex_name
    a.release()
    b.release()


def test_lockfile_roundtrip_stale_detection_and_ownership(data_dir: DataDir) -> None:
    info = LockInfo(pid=os.getpid(), port=1234, token="t", version="0", started_at="now")
    write_lockfile(data_dir, info)
    assert read_lockfile(data_dir) == info
    assert (data_dir.lock_path.stat().st_mode & 0o077) == 0 or os.name == "nt"  # owner-only
    # a lockfile whose process is gone is stale
    write_lockfile(data_dir, info.model_copy(update={"pid": 2**22 + 12345}))
    assert read_lockfile(data_dir) is None
    assert read_lockfile(data_dir, require_alive=False) is not None
    # an engine must not remove another engine's lockfile
    remove_lockfile(data_dir, only_pid=os.getpid())
    assert data_dir.lock_path.exists()
    remove_lockfile(data_dir)
    assert not data_dir.lock_path.exists()
    assert read_lockfile(data_dir) is None
