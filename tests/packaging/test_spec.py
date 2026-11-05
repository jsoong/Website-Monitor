"""``packaging/pagewatch.spec``, executed against stand-ins for PyInstaller's classes.

The spec is Python that PyInstaller runs with ``Analysis``, ``EXE`` and friends injected. Running
it here against recorders checks what it asks PyInstaller to build without building anything; the
real build is ``packaging/build.py`` (``-m packaging`` tests).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests.packaging.helpers import PACKAGING, load_packaging_module

bundle = load_packaging_module("bundle")


class Call:
    def __init__(self, kind: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.kind, self.args, self.kwargs = kind, args, kwargs
        self.pure = f"pure:{id(self)}"
        self.scripts = f"scripts:{id(self)}"
        self.binaries = f"binaries:{id(self)}"
        self.datas = f"datas:{id(self)}"


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.calls: list[Call] = []
        self.monkeypatch = monkeypatch
        self.work = tmp_path / "work"
        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_data_files = lambda pkg, **kw: [(f"/site/{pkg}/data.bin", pkg)]  # type: ignore[attr-defined]
        hooks.collect_submodules = lambda pkg, **kw: [f"{pkg}.dynamic"]  # type: ignore[attr-defined]
        hooks.copy_metadata = lambda pkg, recursive=False: [
            (f"/site/{pkg}.dist-info", pkg, recursive)
        ]  # type: ignore[attr-defined]
        for name, mod in (
            ("PyInstaller", types.ModuleType("PyInstaller")),
            ("PyInstaller.utils", types.ModuleType("PyInstaller.utils")),
            ("PyInstaller.utils.hooks", hooks),
        ):
            monkeypatch.setitem(sys.modules, name, mod)

    def make(self, kind: str) -> Any:
        def factory(*args: Any, **kwargs: Any) -> Call:
            call = Call(kind, args, kwargs)
            self.calls.append(call)
            return call

        return factory

    def run(self) -> None:
        namespace = {
            "__file__": str(PACKAGING / "pagewatch.spec"),
            "SPECPATH": str(PACKAGING),
            "workpath": str(self.work),
            "Analysis": self.make("Analysis"),
            "PYZ": self.make("PYZ"),
            "EXE": self.make("EXE"),
            "COLLECT": self.make("COLLECT"),
        }
        source = (PACKAGING / "pagewatch.spec").read_text(encoding="utf-8")
        exec(compile(source, str(PACKAGING / "pagewatch.spec"), "exec"), namespace)  # noqa: S102

    def of(self, kind: str) -> list[Call]:
        return [c for c in self.calls if c.kind == kind]


@pytest.fixture
def spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    monkeypatch.delenv("PAGEWATCH_NO_UI", raising=False)
    monkeypatch.delenv("PAGEWATCH_VERSION", raising=False)
    return Harness(monkeypatch, tmp_path)


def test_it_builds_three_programs_into_one_folder(spec: Harness) -> None:
    spec.run()
    exes = {c.kwargs["name"]: c for c in spec.of("EXE")}
    assert set(exes) == {"pagewatch-engine", "pagewatch-ui", "pagewatch-cli"}
    (collect,) = spec.of("COLLECT")
    assert collect.kwargs["name"] == "PageWatch"
    # the folder holds every program and every program's libraries, once each
    for exe, analysis in zip(spec.of("EXE"), spec.of("Analysis"), strict=True):
        assert exe in collect.args
        assert analysis.binaries in collect.args and analysis.datas in collect.args


def test_each_program_is_started_from_its_launcher_script(spec: Harness) -> None:
    spec.run()
    scripts = [Path(a.args[0][0]).name for a in spec.of("Analysis")]
    assert scripts == ["engine.py", "ui.py", "cli.py"]


def test_the_engine_and_ui_are_windowed_and_the_cli_has_a_console(spec: Harness) -> None:
    spec.run()
    console = {c.kwargs["name"]: c.kwargs["console"] for c in spec.of("EXE")}
    assert console == {"pagewatch-engine": False, "pagewatch-ui": False, "pagewatch-cli": True}


def test_it_is_a_one_folder_build_without_upx(spec: Harness) -> None:
    spec.run()
    for exe in spec.of("EXE"):
        assert exe.kwargs["exclude_binaries"] is True  # libraries go beside it, not inside it
        assert exe.kwargs["upx"] is False
    assert spec.of("COLLECT")[0].kwargs["upx"] is False


def test_only_the_ui_carries_qt(spec: Harness) -> None:
    spec.run()
    by_script = {Path(a.args[0][0]).stem: a for a in spec.of("Analysis")}
    assert "PySide6" in by_script["engine"].kwargs["excludes"]
    assert "PySide6" in by_script["cli"].kwargs["excludes"]
    assert by_script["ui"].kwargs["excludes"] == []
    assert "pagewatch.ui.main" in by_script["ui"].kwargs["hiddenimports"]
    assert "pagewatch.ui.main" not in by_script["engine"].kwargs["hiddenimports"]


def test_data_files_and_dynamic_imports_reach_every_analysis(spec: Harness) -> None:
    spec.run()
    for analysis in spec.of("Analysis"):
        datas = analysis.kwargs["datas"]
        names = {Path(d[0]).name for d in datas}
        assert "0001_init.sql" in names and "volatile_patterns.yaml" in names
        # Playwright's Node driver has no PyInstaller hook: the spec has to ask for it
        assert ("/site/playwright/data.bin", "playwright") in datas
        # libraries that read their own metadata at import (aioftp did): the whole tree's
        assert any(d[0] == "/site/pagewatch.dist-info" and d[2] is True for d in datas)
        hidden = analysis.kwargs["hiddenimports"]
        assert "uvicorn.dynamic" in hidden  # chosen by name at run time
        assert "pagewatch.engine.pipeline.core" in hidden
        assert "statistics" in hidden  # plugins may import any standard-library module
        assert analysis.kwargs["pathex"] == [str(bundle.SRC)]


def test_a_headless_build_leaves_out_the_ui(spec: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGEWATCH_NO_UI", "1")
    spec.run()
    assert {c.kwargs["name"] for c in spec.of("EXE")} == {"pagewatch-engine", "pagewatch-cli"}
    assert not any(
        "pagewatch.ui" in h for a in spec.of("Analysis") for h in a.kwargs["hiddenimports"]
    )


def test_off_windows_there_is_no_icon_or_version_resource(spec: Harness) -> None:
    spec.run()
    for exe in spec.of("EXE"):
        assert exe.kwargs["icon"] is None and exe.kwargs["version"] is None


def test_on_windows_every_program_gets_the_icon_and_its_own_version_resource(
    spec: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PAGEWATCH_VERSION", "3.4.5")
    spec.run()
    monkeypatch.undo()  # the real platform again before anything else looks at it
    by_name = {c.kwargs["name"]: c for c in spec.of("EXE")}
    for name, exe in by_name.items():
        assert exe.kwargs["icon"].endswith("pagewatch.ico") and Path(exe.kwargs["icon"]).is_file()
        text = Path(exe.kwargs["version"]).read_text(encoding="utf-8")
        assert "3.4.5" in text and f"{name}.exe" in text
    descriptions = {
        n: next(c.description for c in bundle.COMPONENTS if c.exe == n) for n in by_name
    }
    for name, exe in by_name.items():
        assert descriptions[name] in Path(exe.kwargs["version"]).read_text(encoding="utf-8")
