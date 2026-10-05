"""M0 acceptance: a real engine process, a real second engine on the same folder."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir


def _spawn(data: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "pagewatch.engine.main", "--data-dir", str(data),
         "--workers", "thread", "--no-console-log"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def _wait_for_lock(dd: DataDir, proc: subprocess.Popen[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_lockfile(dd) is not None:
            return
        if proc.poll() is not None:
            raise AssertionError(f"engine exited early: {proc.communicate()}")
        time.sleep(0.05)
    raise AssertionError("engine never wrote engine.lock")


def test_engine_serves_health_only_with_token_and_refuses_a_second_instance(
    tmp_path: Path,
) -> None:
    dd = DataDir(tmp_path / "data")
    first = _spawn(dd.root)
    try:
        _wait_for_lock(dd, first)
        info = read_lockfile(dd)
        assert info is not None and info.pid == first.pid
        base = f"http://127.0.0.1:{info.port}"
        assert httpx.get(f"{base}/health").status_code == 401
        ok = httpx.get(f"{base}/health", headers={"Authorization": f"Bearer {info.token}"})
        assert ok.status_code == 200 and ok.json()["pid"] == first.pid

        second = _spawn(dd.root)
        out, err = second.communicate(timeout=30)
        assert second.returncode == 2
        assert "already running" in err and str(dd.root) in err

        assert first.poll() is None  # the first engine was not disturbed
        assert httpx.get(
            f"{base}/health", headers={"Authorization": f"Bearer {info.token}"}
        ).is_success
    finally:
        first.terminate()
        try:
            first.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            first.kill()
    assert first.returncode == 0  # graceful shutdown on SIGTERM
    assert not dd.lock_path.exists()  # lockfile removed on exit
    assert dd.db_path.exists() and (dd.logs_dir / "engine.log").exists()
