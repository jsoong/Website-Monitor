"""``pagewatch.engine.frozen``: starting PageWatch programs from source and from a bundle."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from pagewatch.engine import frozen


def fake_sys(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "executable": "/opt/pagewatch/pagewatch-ui",
        "platform": "linux",
        "stdin": io.StringIO(),
        "stdout": io.StringIO(),
        "stderr": io.StringIO(),
    }
    return SimpleNamespace(**{**base, **over})


# -- command lines ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("component", "module"),
    [
        ("engine", "pagewatch.engine.main"),
        ("ui", "pagewatch.ui.main"),
        ("cli", "pagewatch.cli.main"),
    ],
)
def test_from_source_a_component_is_python_dash_m(component: Any, module: str) -> None:
    cmd = frozen.component_command(component, "--data-dir", "D")
    assert cmd == [sys.executable, "-m", module, "--data-dir", "D"]
    assert not frozen.is_frozen()


def test_packaged_a_component_is_the_sibling_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "pagewatch-ui"
    monkeypatch.setattr(frozen, "sys", fake_sys(frozen=True, executable=str(exe)))
    assert frozen.is_frozen()
    assert frozen.component_command("engine", "--no-console-log") == [
        str(tmp_path / "pagewatch-engine"),
        "--no-console-log",
    ]
    # whichever program asks, the answer is the same folder
    monkeypatch.setattr(frozen, "sys", fake_sys(frozen=True, executable=str(tmp_path / "x-cli")))
    assert frozen.component_command("ui") == [str(tmp_path / "pagewatch-ui")]


def test_packaged_programs_have_an_exe_suffix_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        frozen,
        "sys",
        fake_sys(frozen=True, platform="win32", executable=str(tmp_path / "pagewatch-engine.exe")),
    )
    assert frozen.program_path("cli") == tmp_path / "pagewatch-cli.exe"
    assert frozen.component_command("ui")[0].endswith("pagewatch-ui.exe")


def test_the_playwright_installer_is_the_bundled_node_driver() -> None:
    cmd = frozen.playwright_install_command("chromium")
    node, cli, *rest = cmd.argv
    assert rest == ["install", "chromium"]
    assert Path(node).name in {"node", "node.exe"}
    assert Path(cli).name == "cli.js" and Path(cli).parent.name == "package"
    # next to each other, in playwright's own driver folder: that is what the build collects
    assert Path(node).parent == Path(cli).parent.parent
    assert Path(node).parent.name == "driver" and Path(node).parent.parent.name == "playwright"
    assert cmd.env["PW_LANG_NAME"] == "python"


# -- streams ------------------------------------------------------------------------------


def test_a_windowed_program_gets_usable_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_sys(stdin=None, stdout=None, stderr=None)
    monkeypatch.setattr(frozen, "sys", fake)
    frozen.prepare_streams()
    try:
        print("discarded", file=fake.stderr)  # what the engine does for "already running"
        fake.stdout.write("x")
        assert fake.stdin.read() == ""
    finally:
        for stream in (fake.stdin, fake.stdout, fake.stderr):
            stream.close()


def test_streams_that_exist_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_sys()
    before = (fake.stdin, fake.stdout, fake.stderr)
    monkeypatch.setattr(frozen, "sys", fake)
    frozen.prepare_streams()
    assert (fake.stdin, fake.stdout, fake.stderr) == before


def test_output_goes_to_the_parent_console_when_there_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = io.StringIO()
    fake = fake_sys(stdout=None)
    monkeypatch.setattr(frozen, "sys", fake)
    monkeypatch.setattr(frozen, "_attach_parent_console", lambda: True)
    monkeypatch.setattr(frozen, "_open_console", lambda: console)
    frozen.prepare_streams()
    print("pagewatch-engine 0.1.0", file=fake.stdout)
    assert "0.1.0" in console.getvalue()


def test_a_console_that_cannot_be_opened_falls_back_to_the_null_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_sys(stderr=None)
    monkeypatch.setattr(frozen, "sys", fake)
    monkeypatch.setattr(frozen, "_attach_parent_console", lambda: True)

    def broken() -> Any:
        raise OSError("no console")

    monkeypatch.setattr(frozen, "_open_console", broken)
    frozen.prepare_streams()
    try:
        print("still fine", file=fake.stderr)
    finally:
        fake.stderr.close()


def test_there_is_no_console_to_attach_to_off_windows() -> None:
    assert frozen._attach_parent_console() is False


# -- launch -------------------------------------------------------------------------------


def test_launch_runs_freeze_support_before_it_imports_the_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker process must run its job and exit before the application is imported."""
    calls: list[str] = []
    module = ModuleType("fake_main")
    module.main = lambda argv=None: (calls.append(f"main{argv}"), 7)[1]  # type: ignore[attr-defined]

    def fake_import(name: str) -> ModuleType:
        calls.append(f"import {name}")
        return module

    monkeypatch.setattr(frozen.multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(frozen, "prepare_streams", lambda: calls.append("streams"))
    monkeypatch.setattr(frozen.importlib, "import_module", fake_import)

    assert frozen.launch("engine", ["--version"]) == 7
    assert calls == ["freeze", "streams", "import pagewatch.engine.main", "main['--version']"]


@pytest.mark.parametrize("component", ["engine", "ui", "cli"])
def test_launch_imports_the_right_module(component: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    module = ModuleType("m")
    module.main = lambda argv=None: 0  # type: ignore[attr-defined]
    monkeypatch.setattr(frozen.importlib, "import_module", lambda n: (seen.append(n), module)[1])
    monkeypatch.setattr(frozen, "prepare_streams", lambda: None)
    assert frozen.launch(component) == 0
    assert seen == [frozen.MODULES[component]]


# -- a windowed program, for real ---------------------------------------------------------------

# Linux ignores PyInstaller's ``console=False``, so the packaged smoke test never sees a program
# without a console. Closing descriptors 0-2 before exec makes Python start the way a windowed
# Windows program does: ``sys.stdin``, ``sys.stdout`` and ``sys.stderr`` are all ``None``.
NO_CONSOLE = ["sh", "-c", 'exec "$0" "$@" <&- >&- 2>&-', sys.executable, "-c"]


@pytest.mark.skipif(sys.platform == "win32", reason="closes file descriptors with sh")
def test_launch_gives_a_program_with_no_console_working_streams() -> None:
    """The discriminating one: a stand-in ``main`` that uses all three streams the way the
    engine's start-up messages, logging and libraries might. Without the repair it raises."""
    import subprocess

    probe = (
        "import sys, types\n"
        "assert sys.stdin is None and sys.stdout is None and sys.stderr is None, 'fds were open'\n"
        "from pagewatch.engine import frozen\n"
        "def main(argv=None):\n"
        "    print('hello', file=sys.stderr); sys.stdout.write('x'); sys.stdout.flush()\n"
        "    assert sys.stdin.read() == ''\n"
        "    return 7\n"
        "frozen.importlib.import_module = lambda name: types.SimpleNamespace(main=main)\n"
        "raise SystemExit(frozen.launch('engine'))\n"
    )
    done = subprocess.run([*NO_CONSOLE, probe], timeout=60, check=False)  # noqa: S603
    assert done.returncode == 7, "main() could not use its streams, or never ran"


@pytest.mark.skipif(sys.platform == "win32", reason="closes file descriptors with sh")
def test_a_real_engine_with_no_console_starts_serves_and_stops_cleanly(tmp_path: Path) -> None:
    """The engine itself, started the way Task Scheduler starts the packaged one (no console, and
    console logging left on, so it writes to a stderr that is only the null device). It copes with
    ``None`` streams even without the repair today; this pins that it keeps doing so."""
    import subprocess
    import time

    import httpx

    from pagewatch.engine.instance import read_lockfile
    from pagewatch.engine.paths import DataDir

    data = DataDir(tmp_path / "data")
    code = (
        "from pagewatch.engine.frozen import launch\n"
        f"raise SystemExit(launch('engine', ['--data-dir', {str(data.root)!r}, "
        "'--workers', 'thread', '--no-tray']))\n"
    )
    proc = subprocess.Popen([*NO_CONSOLE, code])  # noqa: S603
    try:
        deadline = time.monotonic() + 45
        info = None
        while time.monotonic() < deadline and info is None:
            assert proc.poll() is None, f"the engine exited with {proc.returncode} while starting"
            info = read_lockfile(data)
            time.sleep(0.05)
        assert info is not None, "the engine never wrote engine.lock"
        health = httpx.get(
            f"http://127.0.0.1:{info.port}/health",
            headers={"Authorization": f"Bearer {info.token}"},
        )
        assert health.is_success
    finally:
        proc.terminate()
        assert proc.wait(timeout=45) == 0
    assert (data.logs_dir / "engine.log").is_file()
