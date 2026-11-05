"""``POST /shutdown`` and ``pagewatch-cli stop``: real engine processes, because what matters is
what the process does (answers, exits 0, removes its lockfile), and the installer relies on it."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from pagewatch.cli import main as cli
from pagewatch.cli.client import ApiError
from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.models import LockInfo


def spawn_engine(data: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "pagewatch.engine.main", "--data-dir", str(data),
         "--workers", "thread", "--no-console-log"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def wait_for_lock(dd: DataDir, proc: subprocess.Popen[str], timeout: float = 30) -> LockInfo:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read_lockfile(dd)
        if info is not None:
            return info
        if proc.poll() is not None:
            raise AssertionError(f"engine exited early: {proc.communicate()}")
        time.sleep(0.05)
    raise AssertionError("engine never wrote engine.lock")


def run_cli(data: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pagewatch.cli.main", "--data-dir", str(data), *args],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    dd = DataDir(tmp_path / "data")
    proc = spawn_engine(dd.root)
    try:
        info = wait_for_lock(dd, proc)
        yield SimpleNamespace(dd=dd, proc=proc, info=info, base=f"http://127.0.0.1:{info.port}")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=30)


def auth(info: LockInfo) -> dict[str, str]:
    return {"Authorization": f"Bearer {info.token}"}


def test_shutdown_is_refused_without_the_token_and_the_engine_keeps_running(engine: Any) -> None:
    assert httpx.post(f"{engine.base}/shutdown").status_code == 401
    wrong = {"Authorization": "Bearer not-the-token"}
    assert httpx.post(f"{engine.base}/shutdown", headers=wrong).status_code == 401
    # a web page cannot do it either, even holding the token: browsers add Origin
    cross_site = {**auth(engine.info), "Origin": "http://evil.example"}
    assert httpx.post(f"{engine.base}/shutdown", headers=cross_site).status_code == 403
    time.sleep(1.0)  # a stop would have started by now
    assert engine.proc.poll() is None
    assert httpx.get(f"{engine.base}/health", headers=auth(engine.info)).is_success


def test_shutdown_answers_then_the_engine_exits_with_code_0_and_removes_its_lockfile(
    engine: Any,
) -> None:
    answer = httpx.post(f"{engine.base}/shutdown", headers=auth(engine.info))
    assert answer.status_code == 202  # delivered, not cut off by the server going down
    assert answer.json()["stopping"] is True
    assert engine.proc.wait(timeout=30) == 0  # "restart me" is 3; a Task Scheduler task stays down
    assert not engine.dd.lock_path.exists()
    assert (engine.dd.logs_dir / "engine.log").read_text().count("engine_stopping") == 1


def test_cli_stop_returns_only_once_the_engine_process_has_exited(engine: Any) -> None:
    done = run_cli(engine.dd.root, "stop")
    assert done.returncode == 0, done.stderr
    assert f"pid {engine.proc.pid}" in done.stdout and "stopped" in done.stdout
    assert cli._exited(engine.proc.pid)  # gone (or collected) the moment `stop` returned
    assert engine.proc.wait(timeout=5) == 0
    assert not engine.dd.lock_path.exists()


def test_cli_stop_with_nothing_running_is_a_success_so_an_installer_can_always_call_it(
    tmp_path: Path,
) -> None:
    done = run_cli(tmp_path / "empty", "stop")
    assert done.returncode == 0 and "not running" in done.stdout


def test_cli_stop_says_so_when_the_engine_does_not_exit_in_time() -> None:
    posts: list[str] = []
    stuck = SimpleNamespace(
        info=SimpleNamespace(pid=os.getpid()),  # a process that is certainly still running
        post=lambda path: posts.append(path),
    )
    started = time.monotonic()
    with pytest.raises(ApiError) as caught:
        cli.stop_engine(stuck, wait_s=0.5)  # type: ignore[arg-type]
    assert posts == ["/shutdown"]
    assert f"pid {os.getpid()}" in caught.value.detail and "still running" in caught.value.detail
    assert 0.4 <= time.monotonic() - started < 5
