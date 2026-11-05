"""Portable install and multiple instances: ``--data-dir`` on engine, UI and CLI (SPEC appendix).

"A second engine may run against a different data folder": two real engine processes, side by
side, each with its own lockfile, port, token and database, and nothing written anywhere but the
two folders (the default location is redirected to a folder the test then checks is empty).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir, default_data_root, resolve_data_dir


def test_data_dir_wins_over_the_environment_and_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAGEWATCH_DATA_DIR", str(tmp_path / "from-env"))
    assert resolve_data_dir(str(tmp_path / "flag")).root == (tmp_path / "flag").resolve()
    assert resolve_data_dir(None).root == (tmp_path / "from-env").resolve()
    monkeypatch.delenv("PAGEWATCH_DATA_DIR")
    assert resolve_data_dir(None).root == default_data_root().resolve()


def test_a_relative_data_dir_is_taken_from_where_the_program_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir("PageWatchData").root == (tmp_path / "PageWatchData").resolve()


def test_the_windows_default_is_local_app_data_pagewatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert default_data_root() == tmp_path / "Local" / "PageWatch"
    monkeypatch.undo()


def _spawn(data_arg: str, cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "pagewatch.engine.main", "--data-dir", data_arg,
         "--workers", "thread", "--no-tray", "--no-console-log"],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def _wait_for_engine(dd: DataDir, proc: subprocess.Popen[str]) -> tuple[str, str]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        info = read_lockfile(dd)
        if info is not None:
            return f"http://127.0.0.1:{info.port}", info.token
        if proc.poll() is not None:
            raise AssertionError(f"engine exited early: {proc.communicate()}")
        time.sleep(0.05)
    raise AssertionError("engine never wrote engine.lock")


def _stop(proc: subprocess.Popen[str]) -> int:
    proc.terminate()
    try:
        proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return proc.returncode


def test_two_engines_run_side_by_side_on_different_folders_and_touch_nothing_else(
    tmp_path: Path,
) -> None:
    home = tmp_path / "default-location"  # where an engine without --data-dir would write
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    other = tmp_path / "elsewhere" / "PageWatch Data"  # an absolute path with a space in it
    env = {
        **os.environ,
        "LOCALAPPDATA": str(home), "XDG_DATA_HOME": str(home), "HOME": str(home),
        "USERPROFILE": str(home), "PAGEWATCH_DATA_DIR": "",
    }  # fmt: skip
    a_dir, b_dir = DataDir(cwd / "data-a"), DataDir(other)  # A is given as a relative path

    a = _spawn("data-a", cwd, env)
    b = _spawn(str(other), cwd, env)
    try:
        a_url, a_token = _wait_for_engine(a_dir, a)
        b_url, b_token = _wait_for_engine(b_dir, b)

        assert a_url != b_url and a_token != b_token
        assert read_lockfile(a_dir).pid == a.pid and read_lockfile(b_dir).pid == b.pid  # type: ignore[union-attr]
        for url, token in ((a_url, a_token), (b_url, b_token)):
            assert httpx.get(
                f"{url}/health", headers={"Authorization": f"Bearer {token}"}
            ).is_success
        # each engine answers only to its own token
        assert (
            httpx.get(f"{a_url}/health", headers={"Authorization": f"Bearer {b_token}"}).status_code
            == 401
        )

        # and each has its own data: a bookmark added to A is not in B
        made = httpx.post(
            f"{a_url}/bookmarks", headers={"Authorization": f"Bearer {a_token}"},
            json={"url": "http://127.0.0.1:9/unused", "name": "only in A", "enabled": False},
        )  # fmt: skip
        assert made.status_code == 201
        listed = {
            "a": httpx.get(f"{a_url}/bookmarks", headers={"Authorization": f"Bearer {a_token}"}),
            "b": httpx.get(f"{b_url}/bookmarks", headers={"Authorization": f"Bearer {b_token}"}),
        }
        assert [x["name"] for x in listed["a"].json()["items"]] == ["only in A"]
        assert listed["b"].json()["items"] == []

        # the CLI follows --data-dir too
        def cli(folder: Path) -> list[dict[str, object]]:
            done = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "pagewatch.cli.main", "--data-dir", str(folder), "--json",
                 "list"], cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=True,
            )  # fmt: skip
            return json.loads(done.stdout)  # type: ignore[no-any-return]

        assert [x["name"] for x in cli(a_dir.root)] == ["only in A"]
        assert cli(b_dir.root) == []
    finally:
        codes = (_stop(a), _stop(b))
    assert codes == (0, 0)  # both shut down cleanly on their own
    assert not a_dir.lock_path.exists() and not b_dir.lock_path.exists()

    # a portable install leaves nothing behind outside its folder
    assert sorted(p.name for p in cwd.iterdir()) == ["data-a"]
    assert sorted(p.name for p in other.parent.iterdir()) == ["PageWatch Data"]
    assert list(home.iterdir()) == [], "something wrote to the default data location"
    for folder in (a_dir, b_dir):
        assert folder.db_path.is_file() and (folder.logs_dir / "engine.log").is_file()
