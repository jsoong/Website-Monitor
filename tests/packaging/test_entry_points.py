"""The three programs: console scripts, frozen launchers and ``pagewatch.engine.frozen`` agree."""

from __future__ import annotations

import ast
import importlib
import re
import runpy
import tomllib
from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine import frozen
from tests.packaging.helpers import PACKAGING, ROOT, SRC, load_packaging_module

bundle = load_packaging_module("bundle")

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_console_scripts_launcher_and_frozen_table_name_the_same_modules() -> None:
    scripts = PYPROJECT["project"]["scripts"]
    for key, program in frozen.PROGRAMS.items():
        assert scripts[program] == f"{frozen.MODULES[key]}:main"
    assert set(scripts) == set(frozen.PROGRAMS.values())


def test_the_bundle_table_agrees_with_the_frozen_table() -> None:
    assert {c.key: c.exe for c in bundle.COMPONENTS} == frozen.PROGRAMS


@pytest.mark.parametrize("key", sorted(frozen.MODULES))
def test_every_entry_module_has_a_callable_main(key: Any) -> None:
    if key == "ui":
        pytest.importorskip("PySide6")
    module = importlib.import_module(frozen.MODULES[key])
    assert callable(module.main)


@pytest.mark.parametrize("key", sorted(frozen.MODULES))
def test_a_launcher_only_calls_launch_under_a_main_guard(key: Any) -> None:
    """freeze_support() must run before anything else: no other imports or statements."""
    tree = ast.parse((PACKAGING / "entry" / f"{key}.py").read_text(encoding="utf-8"))
    body = [
        n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
    ]
    assert len(body) == 2, "an import of launch and the __main__ guard, nothing else"
    imp, guard = body
    assert isinstance(imp, ast.ImportFrom) and imp.module == "pagewatch.engine.frozen"
    assert [a.name for a in imp.names] == ["launch"]
    assert isinstance(guard, ast.If) and "__name__" in ast.unparse(guard.test)


@pytest.mark.parametrize("key", sorted(frozen.MODULES))
def test_a_launcher_exits_with_what_launch_returns(
    key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        frozen, "launch", lambda component, argv=None: (seen.append(component), 5)[1]
    )
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(PACKAGING / "entry" / f"{key}.py"), run_name="__main__")
    assert stop.value.code == 5 and seen == [key]


def test_a_launcher_does_nothing_when_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under `spawn` a worker re-imports the main script as `__mp_main__`; it must not launch."""
    monkeypatch.setattr(frozen, "launch", lambda *a, **k: pytest.fail("launched on import"))
    runpy.run_path(str(PACKAGING / "entry" / "engine.py"), run_name="__mp_main__")


def test_all_three_programs_take_data_dir_for_portable_mode() -> None:
    """SPEC appendix: portable install and multiple instances, ``--data-dir`` on all three."""
    from pagewatch.cli.main import build_parser as cli_parser
    from pagewatch.engine.main import build_parser as engine_parser
    from pagewatch.ui.main import build_parser as ui_parser

    for build in (engine_parser, cli_parser, ui_parser):
        parser = build()
        args = parser.parse_args(["--data-dir", "D", *(["status"] if build is cli_parser else [])])
        assert args.data_dir == "D"


def test_nothing_in_the_package_spawns_python_dash_m() -> None:
    """Why the audit exists: from a bundle there is no `python` to run `-m` with.

    A use that really is source-only says so with ``# frozen-ok: <why>`` on its line or the line
    above (the formatter may wrap a line and strand a trailing comment).
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "frozen.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if "sys.executable" not in line.split("#", 1)[0]:
                continue
            nearby = line + (lines[number - 2] if number > 1 else "")
            if "frozen-ok" not in nearby:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "These start a child process with sys.executable, which in the packaged build is the "
        "running pagewatch program, not Python:\n  "
        + "\n  ".join(offenders)
        + "\nUse pagewatch.engine.frozen.component_command(...) (or playwright_install_command()). "
        "If the use really is source-only, add `# frozen-ok: <why>` to the line or the one above."
    )


def test_data_files_are_not_found_by_walking_up_from_dunder_file() -> None:
    """`importlib.resources` works in a bundle (the spec collects the files); a path built from
    ``__file__`` points somewhere else once the modules live in PyInstaller's archive."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "__file__" in line.split("#", 1)[0]:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_resources_are_read_only_from_files_the_bundle_includes() -> None:
    """Every ``resources.files("pkg") / "name"`` in the code names a file the spec collects."""
    bundled = {
        (dest.replace("/", "."), Path(src).name) for src, dest in bundle.package_data_files()
    }
    bundled_dirs = {dest for dest, _ in bundled}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for package in re.findall(r'resources\.files\(\s*"([\w.]+)"\s*\)', text):
            assert any(d == package or d.startswith(package + ".") for d in bundled_dirs), (
                f"{path.relative_to(ROOT)} reads resources of {package}, but the bundle has no "
                f"data files there"
            )
