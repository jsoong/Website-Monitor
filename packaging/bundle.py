"""What goes into the PyInstaller bundle, as plain Python.

``pagewatch.spec`` hands this to PyInstaller and ``tests/packaging`` checks it, so the decisions
(which programs, which data files, which hidden imports) are written down once and can be tested on
any machine without PyInstaller. Do not add ``__init__.py`` to this folder: it is called
``packaging`` because the spec names it, and as a package it would shadow PyPI's ``packaging``.
Load it by path (``importlib.util.spec_from_file_location``), as the spec and the tests do.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PACKAGE = SRC / "pagewatch"
ENTRY = Path(__file__).resolve().parent / "entry"
ICON = Path(__file__).resolve().parent / "assets" / "pagewatch.ico"

#: The folder inside ``dist/`` that holds the three programs and ``_internal/``.
BUNDLE_NAME = "PageWatch"

PRODUCT_NAME = "PageWatch"


@dataclass(frozen=True)
class Component:
    """One program in the bundle (the SPEC's three entry points)."""

    key: str  # engine | ui | cli: the argument of pagewatch.engine.frozen.launch
    exe: str  # file name without .exe; the same as the [project.scripts] name
    description: str  # Windows "File description": shown in Task Manager and Task Scheduler
    console: bool  # False = windowed: no console window (engine at logon, UI)
    needs_ui: bool  # carries PySide6 and pagewatch.ui

    @property
    def script(self) -> Path:
        return ENTRY / f"{self.key}.py"

    @property
    def excludes(self) -> tuple[str, ...]:
        """Modules PyInstaller must not pull in for this program."""
        return () if self.needs_ui else UI_ONLY_PACKAGES


#: Third-party packages only the UI uses; the engine and the CLI stay small without them.
UI_ONLY_PACKAGES = ("PySide6", "shiboken6", "pagewatch.ui")

COMPONENTS: tuple[Component, ...] = (
    # The engine starts at logon with no terminal, so it is windowed.
    Component("engine", "pagewatch-engine", "PageWatch engine", console=False, needs_ui=False),
    Component("ui", "pagewatch-ui", "PageWatch", console=False, needs_ui=True),
    # The CLI is run from a terminal and by the installer; it needs a console for its output.
    Component("cli", "pagewatch-cli", "PageWatch command line", console=True, needs_ui=False),
)


# -- version ------------------------------------------------------------------------------


def read_version(src: Path = PACKAGE) -> str:
    """``pagewatch.__version__`` read from the file (not imported: the build must not need the
    package importable to learn its own version)."""
    text = (src / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise RuntimeError(f"no __version__ in {src / '__init__.py'}")
    return m.group(1)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """``0.2.1`` -> ``(0, 2, 1, 0)``; ``0.2.1rc1`` -> ``(0, 2, 1, 0)``. The Windows version
    resource holds four numbers."""
    nums: list[int] = []
    for part in version.split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        nums.append(int(m.group(0)))
        if m.end() != len(part):  # "0rc1", "0-beta": the number ends the numeric part
            break
    nums = (nums + [0, 0, 0, 0])[:4]
    return nums[0], nums[1], nums[2], nums[3]


def version_info(component: Component, version: str) -> str:
    """The text of a PyInstaller Windows version resource for ``component``."""
    a, b, c, d = version_tuple(version)
    dotted = f"{a}.{b}.{c}.{d}"

    def s(key: str, value: str) -> str:
        return f"StringStruct('{key}', '{value}')"

    strings = ", ".join(
        [
            s("CompanyName", PRODUCT_NAME),
            s("FileDescription", component.description),
            s("FileVersion", dotted),
            s("InternalName", component.exe),
            s("OriginalFilename", f"{component.exe}.exe"),
            s("ProductName", PRODUCT_NAME),
            s("ProductVersion", version),
        ]
    )
    return (
        "VSVersionInfo(\n"
        f"  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, {d}), prodvers=({a}, {b}, {c}, {d}),"
        " mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[\n"
        f"    StringFileInfo([StringTable('040904B0', [{strings}])]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )


# -- files and modules --------------------------------------------------------------------

_CODE_SUFFIXES = {".py", ".pyc", ".pyo"}


def package_data_files(package: Path = PACKAGE) -> list[tuple[str, str]]:
    """Every non-code file under the ``pagewatch`` package as PyInstaller ``datas``:
    ``(file, folder inside the bundle)``.

    PyInstaller follows imports, not ``importlib.resources``, so without this the SQL migrations,
    ``data/*.yaml`` and ``ui/web/diff.css`` would be missing from the bundle. It walks the tree
    rather than listing files so whatever a later milestone adds (report templates, plugin
    examples, ...) ships without anyone having to remember this file.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(package.rglob("*")):
        if not path.is_file() or path.suffix in _CODE_SUFFIXES or "__pycache__" in path.parts:
            continue
        found.append((str(path), str(path.parent.relative_to(package.parent)).replace("\\", "/")))
    return found


def package_modules(package: Path = PACKAGE, *, with_ui: bool = True) -> list[str]:
    """Dotted names of every module under ``pagewatch``, for PyInstaller's ``hiddenimports``.

    Several are reached only dynamically (a worker process unpickles a function by module name;
    plugins and later milestones add modules), so none is left to import analysis.
    """
    names: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(package.parent).with_suffix("")
        parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
        name = ".".join(parts)
        if not with_ui and (name == "pagewatch.ui" or name.startswith("pagewatch.ui.")):
            continue
        names.append(name)
    return names


#: Packages whose submodules are chosen at run time (by string or entry point), so import
#: analysis cannot see them. Collected only when installed: the Windows-only ones are not
#: present in a Linux build, and ``keyring`` arrives with a later milestone.
DYNAMIC_PACKAGES = (
    "uvicorn",  # loops, protocols and the websockets implementation are picked by name
    "websockets",
    "anyio",
    "httpx",
    "h2",  # httpx[http2]
    "hpack",
    "hyperframe",
    "keyring",  # backends are discovered at run time
    "win32ctypes",
    "pystray",  # the backend module is imported by platform name
    "windows_toasts",  # a tree of winrt projections
    "winrt",
    "jinja2",
)

#: Packages that ship data files the import system does not see. ``playwright`` is the important
#: one: its Node driver is what talks to Edge, and PyInstaller has no hook for it.
DATA_PACKAGES = ("playwright", "pdfminer", "docx", "openpyxl", "tzdata", "certifi")

#: Standard-library modules that are never wanted in a bundle.
_STDLIB_SKIP = {
    "tkinter", "turtle", "turtledemo", "idlelib", "lib2to3", "test", "antigravity", "this",
    "ensurepip", "venv", "pydoc_data", "__main__", "__future__",
}  # fmt: skip


def stdlib_modules() -> list[str]:
    """Public standard-library modules available to this interpreter.

    Plugins are "trusted local code" loaded from the data folder at run time, so they may import
    anything the standard library offers, not just what the application itself happens to use.
    PyInstaller keeps only the latter unless told otherwise.
    """
    names = []
    for name in sorted(sys.stdlib_module_names):
        if name.startswith("_") or name in _STDLIB_SKIP:
            continue
        try:
            found = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if found is not None:
            names.append(name)
    return names


def is_installed(package: str) -> bool:
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def hidden_imports(component: Component) -> list[str]:
    """Modules to force into ``component``'s archive (third-party submodules are added by the
    spec, which can ask PyInstaller to enumerate them)."""
    return [*package_modules(with_ui=component.needs_ui), *stdlib_modules()]
