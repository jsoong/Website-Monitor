"""Build the real bundle with PyInstaller and run it. Opt in: ``pytest -m packaging``.

This is the test of the thing users run, and the one that finds the failures the others cannot: a
library that reads its own metadata at import, a data file PyInstaller did not copy, a worker
process that re-runs the application. Two minutes for the headless build, a few more with the UI
(and ~1 GB of disk in the temporary folder).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.packaging.helpers import load_packaging_module

build = load_packaging_module("build")
smoke = load_packaging_module("smoke")
bundle = load_packaging_module("bundle")

pytestmark = pytest.mark.packaging


@pytest.fixture(scope="module")
def headless(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """dist/ of a build without the UI, smoke-tested, with the portable zip."""
    root = tmp_path_factory.mktemp("pyinstaller-headless")
    code = build.main(
        ["--out", str(root / "dist"), "--work", str(root / "work"), "--no-ui", "--smoke",
         "--portable"]
    )  # fmt: skip
    assert code == 0, "build or smoke test failed (see the captured output)"
    return root / "dist"


def test_the_headless_bundle_has_the_programs_and_passes_the_smoke_test(headless: Path) -> None:
    folder = headless / bundle.BUNDLE_NAME
    for name in ("pagewatch-engine", "pagewatch-cli"):
        assert (folder / name).exists() or (folder / f"{name}.exe").exists()
    assert (folder / "USER_GUIDE.md").is_file(), "the installer links to it"
    assert (folder / "_internal").is_dir()


def test_the_portable_zip_is_a_complete_runnable_copy(headless: Path, tmp_path: Path) -> None:
    version = bundle.read_version()
    zip_path = headless / build.portable_name(version)
    sidecar = zip_path.with_name(zip_path.name + ".sha256")
    assert sidecar.read_text(encoding="ascii").split(" *")[0] == build.sha256_of(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp_path)
    unpacked = tmp_path / f"PageWatch-{version}-portable"
    assert (unpacked / "Start PageWatch.cmd").is_file()
    assert (unpacked / "README-portable.txt").is_file()
    # the programs inside the zip run, not just exist (zip keeps no Unix modes: restore them)
    for path in unpacked.rglob("*"):
        if path.is_file() and (path.name.startswith("pagewatch-") or path.name == "node"):
            path.chmod(0o755)
    assert smoke.main([str(unpacked), "--expect-version", version]) == 0


def test_the_full_bundle_with_the_ui_starts_the_ui(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    code = build.main(
        ["--out", str(tmp_path / "dist"), "--work", str(tmp_path / "work"), "--smoke"]
    )
    assert code == 0, "build or smoke test (including the UI stage) failed"
