"""The places M5 starts the engine (the Task Scheduler task and the self-respawn) in a bundle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pagewatch.cli import service
from pagewatch.engine import frozen
from pagewatch.engine import main as engine_main


def bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(
        frozen,
        "sys",
        SimpleNamespace(
            frozen=True, platform="win32", executable=str(tmp_path / "pagewatch-cli.exe")
        ),
    )
    return tmp_path


def test_the_task_runs_the_packaged_engine_even_without_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = bundle(monkeypatch, tmp_path)
    program, arguments = service.engine_command(None)
    assert program == str(folder / "pagewatch-engine.exe")
    assert "-m" not in arguments and "--supervised" in arguments


def test_an_explicit_exe_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle(monkeypatch, tmp_path)
    program, _ = service.engine_command(None, exe="D:\\PageWatch\\pagewatch-engine.exe")
    assert program == "D:\\PageWatch\\pagewatch-engine.exe"


def test_from_source_the_task_still_runs_python_dash_m() -> None:
    program, arguments = service.engine_command(None)
    assert "-m pagewatch.engine.main" in arguments and Path(program).stem.startswith("python")


def test_a_respawned_engine_is_the_packaged_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = bundle(monkeypatch, tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(engine_main.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd))
    monkeypatch.delenv(engine_main.RESPAWN_ENV, raising=False)
    assert engine_main.respawn(["--data-dir", "D"]) is True
    assert seen == [[str(folder / "pagewatch-engine.exe"), "--data-dir", "D"]]
