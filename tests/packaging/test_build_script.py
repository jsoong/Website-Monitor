"""``packaging/build.py``, ``tools/make_icon.py`` and ``packaging/smoke.py``'s helpers.

Not the real build (that is ``-m packaging``): the command lines it would run, the artifacts it
names, and the small tools around it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import struct
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from tests.packaging.helpers import PACKAGING, ROOT, load_packaging_module

build = load_packaging_module("build")
bundle = load_packaging_module("bundle")
smoke = load_packaging_module("smoke")


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests look at command lines; none may ever run one (a real build takes minutes)."""

    def refuse(cmd: list[str], **_kw: object) -> None:
        raise AssertionError(f"a test tried to run {cmd[:3]}")

    monkeypatch.setattr(build, "run", refuse)


# -- commands -----------------------------------------------------------------------------


def test_pyinstaller_is_run_through_this_interpreter_on_the_spec(tmp_path: Path) -> None:
    cmd = build.pyinstaller_command(tmp_path / "dist", tmp_path / "work", clean=False)
    assert cmd[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--noconfirm" in cmd and "--clean" not in cmd
    assert cmd[cmd.index("--distpath") + 1] == str(tmp_path / "dist")
    assert cmd[cmd.index("--workpath") + 1] == str(tmp_path / "work")
    assert cmd[-1] == str(PACKAGING / "pagewatch.spec")
    assert "--clean" in build.pyinstaller_command(tmp_path, tmp_path, clean=True)


def test_the_inno_compiler_command_passes_what_the_script_requires(tmp_path: Path) -> None:
    cmd = build.iscc_command(Path("C:/is/ISCC.exe"), "1.2.3", tmp_path / "PageWatch", tmp_path)
    assert "/DAppVersion=1.2.3" in cmd
    assert f"/DSourceDir={tmp_path / 'PageWatch'}" in cmd
    assert f"/O{tmp_path}" in cmd  # Inno wants no space after /O
    assert cmd[-1] == str(PACKAGING / "installer.iss")
    iss = (PACKAGING / "installer.iss").read_text(encoding="utf-8")
    for define in ("AppVersion", "SourceDir"):
        assert f"/D{define}=" in " ".join(cmd) and f"{define}" in iss


def test_artifact_names() -> None:
    assert build.installer_name("0.1.0") == "PageWatch-Setup-0.1.0.exe"
    assert build.portable_name("0.1.0") == "PageWatch-0.1.0-portable.zip"


def test_the_inno_compiler_is_found_from_the_environment_the_path_or_the_usual_folders(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "ISCC.exe"
    fake.write_bytes(b"")
    fake.chmod(0o755)  # `which` wants the executable bit off Windows
    assert build.find_iscc({"ISCC": str(fake)}) == fake
    assert build.find_iscc({"PATH": str(tmp_path)}) == fake  # on PATH
    pf = tmp_path / "Program Files (x86)"
    (pf / "Inno Setup 6").mkdir(parents=True)
    (pf / "Inno Setup 6" / "ISCC.exe").write_bytes(b"")
    assert build.find_iscc({"ProgramFiles(x86)": str(pf), "PATH": ""}) == (
        pf / "Inno Setup 6" / "ISCC.exe"
    )
    assert build.find_iscc({"PATH": "", "ISCC": str(tmp_path / "missing.exe")}) is None


# -- checksum and zip -----------------------------------------------------------------------


def test_the_checksum_file_is_in_the_format_sha256sum_and_get_filehash_use(tmp_path: Path) -> None:
    f = tmp_path / "PageWatch-Setup-0.1.0.exe"
    f.write_bytes(b"installer bytes" * 1000)
    sidecar = build.write_checksum(f)
    assert sidecar.name == f.name + ".sha256"
    digest, name = sidecar.read_text(encoding="ascii").strip().split(" *")
    assert digest == hashlib.sha256(f.read_bytes()).hexdigest() and name == f.name


def test_the_portable_zip_holds_the_build_and_the_launcher_under_one_folder(tmp_path: Path) -> None:
    folder = tmp_path / "PageWatch"
    (folder / "_internal").mkdir(parents=True)
    for name in ("pagewatch-engine.exe", "pagewatch-ui.exe", "pagewatch-cli.exe"):
        (folder / name).write_bytes(b"MZ")
    (folder / "_internal" / "base_library.zip").write_bytes(b"PK")
    out = build.make_portable_zip(folder, tmp_path / "out" / "p.zip", "0.1.0")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert zf.testzip() is None
    top = "PageWatch-0.1.0-portable/"
    assert all(n.startswith(top) for n in names)
    assert top + "pagewatch-engine.exe" in names and top + "_internal/base_library.zip" in names
    # the launcher and its readme come from packaging/portable
    portable = {p.name for p in (PACKAGING / "portable").iterdir() if p.is_file()}
    assert portable and {top + n for n in portable} <= set(names)


def test_the_build_refuses_to_start_without_the_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    # build.py loads its own copy of bundle.py, so that is the one to patch
    monkeypatch.setattr(build.bundle, "ICON", tmp_path / "missing.ico")
    args = argparse.Namespace(
        out=tmp_path, work=tmp_path, no_ui=True, clean=False, smoke=False, portable=False,
        installer=False,
    )  # fmt: skip
    with pytest.raises(build.BuildError, match="make_icon"):
        build.build(args)


# -- the icon -------------------------------------------------------------------------------


def test_the_committed_icon_has_every_size_windows_asks_for() -> None:
    data = (PACKAGING / "assets" / "pagewatch.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = {data[6 + 16 * i] or 256 for i in range(count)}
    assert sizes == {16, 24, 32, 48, 64, 128, 256}


def test_the_icon_generator_reproduces_the_committed_file(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "tools" / "make_icon.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = module.write_ico(tmp_path / "icon.ico")
    assert out.read_bytes() == (PACKAGING / "assets" / "pagewatch.ico").read_bytes()


# -- the smoke script's own pieces ----------------------------------------------------------


def test_the_page_server_serves_what_the_test_sets() -> None:
    with smoke.PageServer() as page:
        first = urllib.request.urlopen(page.url, timeout=10).read()  # noqa: S310
        page.body = b"<html>changed</html>"
        second = urllib.request.urlopen(page.url, timeout=10).read()  # noqa: S310
    assert b"first version" in first and second == b"<html>changed</html>"


def test_wait_until_returns_the_value_and_times_out_with_what_it_waited_for() -> None:
    assert smoke.wait_until("x", lambda: "done", 1) == "done"
    started = time.monotonic()
    with pytest.raises(smoke.SmokeFailure, match="waiting for the thing"):
        smoke.wait_until("the thing", lambda: False, 0.3, interval_s=0.05)
    assert time.monotonic() - started < 3


def test_wait_until_reports_the_last_error_and_lets_real_failures_through() -> None:
    def boom() -> None:
        raise ConnectionError("refused")

    with pytest.raises(smoke.SmokeFailure, match="refused"):
        smoke.wait_until("a server", boom, 0.2, interval_s=0.05)

    def fatal() -> None:
        raise smoke.SmokeFailure("the engine exited")

    with pytest.raises(smoke.SmokeFailure, match="the engine exited"):
        smoke.wait_until("a server", fatal, 5)  # not retried until the timeout


def test_the_smoke_test_says_what_is_missing_from_an_empty_folder(tmp_path: Path) -> None:
    assert smoke.main([str(tmp_path)]) == 1  # layout fails first, nothing is started


@pytest.mark.skipif(sys.platform == "win32", reason="uses ps")
def test_the_process_list_is_not_truncated_by_the_columns_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI stage looks for QtWebEngineProcess at the end of a long install path; with COLUMNS
    set (pytest, CI) `ps` cut the line short and the stage failed on a healthy bundle."""
    import subprocess

    marker = "x" * 150 + "-the-end-of-a-long-command-line"
    monkeypatch.setenv("COLUMNS", "40")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])  # noqa: S603
    try:
        assert marker in smoke.running_processes()
    finally:
        child.kill()
        child.wait()
