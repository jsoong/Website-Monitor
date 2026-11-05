"""``packaging/bundle.py``: what goes into the PyInstaller bundle."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.packaging.helpers import PACKAGING, ROOT, SRC, load_packaging_module

bundle = load_packaging_module("bundle")


# -- data files ---------------------------------------------------------------------------


def test_the_files_read_through_importlib_resources_are_bundled() -> None:
    """PyInstaller follows imports, not ``importlib.resources``: these would be silently absent."""
    datas = {Path(src).name: dest for src, dest in bundle.package_data_files()}
    assert datas["0001_init.sql"] == "pagewatch/engine/store/migrations"
    assert datas["0002_logins.sql"] == "pagewatch/engine/store/migrations"
    assert datas["volatile_patterns.yaml"] == "pagewatch/data"
    assert datas["cookie_banner_selectors.txt"] == "pagewatch/data"
    assert datas["diff.css"] == "pagewatch/ui/web"


def test_every_non_code_file_in_the_package_is_bundled() -> None:
    """Whatever a later milestone adds (templates, plugin examples) ships without anyone editing
    the build: compare against an independent walk of the tree."""
    on_disk = {
        Path(root, name)
        for root, dirs, files in os.walk(SRC)
        if "__pycache__" not in root
        for name in files
        if not name.endswith((".py", ".pyc", ".pyo"))
    }
    bundled = {Path(src) for src, _ in bundle.package_data_files()}
    assert bundled == on_disk
    assert on_disk, "the package has data files, so this test is checking something"


def test_data_destinations_are_relative_forward_slash_folders() -> None:
    for src, dest in bundle.package_data_files():
        assert Path(src).is_file()
        assert not Path(dest).is_absolute() and "\\" not in dest
        assert dest.startswith("pagewatch")
        assert (SRC.parent / dest / Path(src).name).is_file()  # dest really mirrors the tree


# -- modules ------------------------------------------------------------------------------


def test_every_module_is_listed_for_the_ui_build_and_the_ui_is_left_out_of_the_rest() -> None:
    everything = bundle.package_modules()
    headless = bundle.package_modules(with_ui=False)
    for name in (
        "pagewatch",
        "pagewatch.models",
        "pagewatch.engine.main",
        "pagewatch.engine.pipeline.core",  # unpickled by name in a worker process
        "pagewatch.engine.store",
        "pagewatch.cli.main",
    ):
        assert name in headless, name
    assert "pagewatch.ui.main" in everything and "pagewatch.ui.windows.main_window" in everything
    assert not [n for n in headless if n == "pagewatch.ui" or n.startswith("pagewatch.ui.")]
    assert set(headless) < set(everything)


@pytest.mark.parametrize("with_ui", [False, True])
def test_every_listed_module_imports(with_ui: bool) -> None:
    """A typo or a stale name in the hidden imports would only show up in the packaged app."""
    if with_ui:
        pytest.importorskip("PySide6")
    for name in bundle.package_modules(with_ui=with_ui):
        if name.startswith("pagewatch.ui") and not with_ui:
            continue
        importlib.import_module(name)


def test_the_standard_library_is_available_to_plugins() -> None:
    std = set(bundle.stdlib_modules())
    assert {"statistics", "csv", "zipfile", "html", "urllib", "sqlite3", "json", "re"} <= std
    assert not {"tkinter", "idlelib", "test", "turtle"} & std
    assert not [n for n in std if n.startswith("_")]


def test_hidden_imports_differ_only_by_the_ui() -> None:
    engine, ui, cli = (bundle.hidden_imports(c) for c in bundle.COMPONENTS)
    assert "pagewatch.ui.main" in ui and "pagewatch.ui.main" not in engine
    assert engine == cli  # the engine and the CLI need the same set


# -- the three programs -------------------------------------------------------------------


def test_the_three_programs_match_the_projects_console_scripts() -> None:
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    assert {c.exe for c in bundle.COMPONENTS} == set(scripts)


def test_console_flags_follow_how_each_program_is_started() -> None:
    by_key = {c.key: c for c in bundle.COMPONENTS}
    assert not by_key["engine"].console, "starts at logon: no console window may flash up"
    assert not by_key["ui"].console
    assert by_key["cli"].console, "run from a terminal and by the installer: needs its output"
    assert by_key["ui"].needs_ui and not by_key["engine"].needs_ui and not by_key["cli"].needs_ui
    assert "PySide6" in by_key["engine"].excludes and not by_key["ui"].excludes


def test_every_program_has_a_launcher_script() -> None:
    for comp in bundle.COMPONENTS:
        assert comp.script.is_file(), comp.script


def test_the_packaging_folder_is_not_a_python_package() -> None:
    """It would shadow PyPI's ``packaging``, which pytest and pip need."""
    assert not (PACKAGING / "__init__.py").exists()
    import packaging.version

    # the installed library, not this folder, is what `import packaging` finds
    assert PACKAGING not in Path(packaging.version.__file__).parents


# -- version ------------------------------------------------------------------------------


def test_the_version_in_the_code_and_in_pyproject_agree() -> None:
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    import pagewatch

    assert bundle.read_version() == pagewatch.__version__ == declared


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1.0", (0, 1, 0, 0)),
        ("1.2.3.4", (1, 2, 3, 4)),
        ("2", (2, 0, 0, 0)),
        ("0.2.0rc1", (0, 2, 0, 0)),
        ("1.0.0-beta.2", (1, 0, 0, 0)),  # a pre-release number is not a fourth component
        ("1.2.3.4.5", (1, 2, 3, 4)),
    ],
)
def test_version_tuple(text: str, expected: tuple[int, int, int, int]) -> None:
    assert bundle.version_tuple(text) == expected


def _pyinstaller_signatures() -> dict[str, list[tuple[str, bool]]]:
    """Constructor parameters of PyInstaller's version-resource classes, read from its source
    (importing the module needs ``pefile``, which only installs on Windows)."""
    spec = importlib.util.find_spec("PyInstaller")
    assert spec is not None and spec.origin is not None
    path = Path(spec.origin).parent / "utils" / "win32" / "versioninfo.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, list[tuple[str, bool]]] = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"):
            args = fn.args.args[1:]
            n_default = len(fn.args.defaults)
            found[cls.name] = [(a.arg, i >= len(args) - n_default) for i, a in enumerate(args)]
    return found


def _evaluate_version_info(text: str) -> Any:
    sigs = _pyinstaller_signatures()
    for needed in (
        "VSVersionInfo", "FixedFileInfo", "StringFileInfo", "StringTable", "StringStruct",
        "VarFileInfo", "VarStruct",
    ):  # fmt: skip
        assert needed in sigs, f"PyInstaller no longer has {needed}"

    def stub(name: str) -> Any:
        params = [
            inspect.Parameter(
                p, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None if optional else inspect.Parameter.empty,
            )
            for p, optional in sigs[name]
        ]  # fmt: skip
        signature = inspect.Signature(params)

        def call(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)  # raises TypeError on a wrong call
            return (name, dict(bound.arguments))

        return call

    return eval(text, {name: stub(name) for name in sigs})  # noqa: S307 - our own text


def test_the_windows_version_resource_is_what_pyinstaller_expects() -> None:
    comp = next(c for c in bundle.COMPONENTS if c.key == "engine")
    name, top = _evaluate_version_info(bundle.version_info(comp, "0.1.0"))
    assert name == "VSVersionInfo"
    assert top["ffi"][1]["filevers"] == (0, 1, 0, 0) == top["ffi"][1]["prodvers"]
    strings = {s[1]["name"]: s[1]["val"] for s in top["kids"][0][1]["kids"][0][1]["kids"]}
    assert strings["FileDescription"] == "PageWatch engine"  # what Task Manager shows
    assert strings["OriginalFilename"] == "pagewatch-engine.exe"
    assert strings["ProductVersion"] == "0.1.0" and strings["FileVersion"] == "0.1.0.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", strings["FileVersion"])


@pytest.mark.parametrize("comp", bundle.COMPONENTS, ids=lambda c: c.key)
def test_each_program_has_a_valid_version_resource(comp: Any) -> None:
    _evaluate_version_info(bundle.version_info(comp, "12.0.3rc1"))
