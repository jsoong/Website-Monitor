# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: PageWatch as one folder with three programs.
#
#   dist/PageWatch/
#       pagewatch-engine.exe   always on; windowed (no console), starts at logon
#       pagewatch-ui.exe       desktop client; windowed; carries PySide6 and QtWebEngine
#       pagewatch-cli.exe      command line; console
#       _internal/             the shared Python runtime, libraries and data files
#
# Build with `uv run python packaging/build.py` (see packaging/README.md); do not call PyInstaller
# on this file directly unless you also set the PAGEWATCH_* variables it reads. What goes in is
# decided in packaging/bundle.py, which is plain Python so the tests can check it.
#
# `Analysis`, `PYZ`, `EXE`, `COLLECT`, `SPECPATH` and `workpath` are provided by PyInstaller.

import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

SPEC_DIR = Path(SPECPATH)  # noqa: F821


def _load(name):
    # By path: this folder is called "packaging" and must not become an importable package.
    spec = importlib.util.spec_from_file_location(f"pagewatch_{name}", SPEC_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bundle = _load("bundle")

VERSION = os.environ.get("PAGEWATCH_VERSION") or bundle.read_version()
# PAGEWATCH_NO_UI=1 leaves out the UI (and PySide6, the bulk of the size): a quick build for
# checking the engine and the CLI, e.g. in CI. A release build never sets it.
WITH_UI = os.environ.get("PAGEWATCH_NO_UI") != "1"
IS_WINDOWS = sys.platform == "win32"

# Third-party packages whose submodules are chosen at run time, and those with data files.
third_party_imports = []
for package in bundle.DYNAMIC_PACKAGES:
    if bundle.is_installed(package):
        third_party_imports += collect_submodules(package)

third_party_data = []
for package in bundle.DATA_PACKAGES:
    if bundle.is_installed(package):
        third_party_data += collect_data_files(package)

# Several libraries read their own version or entry points through importlib.metadata when they
# are imported (aioftp does, keyring discovers its backends that way), and PyInstaller leaves
# the .dist-info folders out. Copying them for pagewatch and everything it depends on is a few
# hundred kilobytes and avoids a crash at start-up that only the packaged build has.
third_party_data += copy_metadata("pagewatch", recursive=True)

own_data = bundle.package_data_files()

version_dir = Path(workpath) / "version"  # noqa: F821
version_dir.mkdir(parents=True, exist_ok=True)

parts = []  # [exe, analysis]
for comp in bundle.COMPONENTS:
    if comp.needs_ui and not WITH_UI:
        continue

    a = Analysis(  # noqa: F821
        [str(comp.script)],
        pathex=[str(bundle.SRC)],
        binaries=[],
        datas=own_data + third_party_data,
        hiddenimports=bundle.hidden_imports(comp) + third_party_imports,
        hookspath=[],
        runtime_hooks=[],
        excludes=list(comp.excludes),
        noarchive=False,
    )
    pyz = PYZ(a.pure)  # noqa: F821

    version_file = None
    if IS_WINDOWS:  # the version resource and icon only exist on Windows
        version_file = version_dir / f"{comp.exe}.txt"
        version_file.write_text(bundle.version_info(comp, VERSION), encoding="utf-8")

    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,  # one-folder build: the libraries live in _internal
        name=comp.exe,
        console=comp.console,
        icon=str(bundle.ICON) if IS_WINDOWS else None,
        version=str(version_file) if version_file else None,
        upx=False,  # UPX slows start-up and makes antivirus products suspicious
        disable_windowed_traceback=False,
    )
    parts.append((exe, a))

coll = COLLECT(  # noqa: F821
    *[item for exe, a in parts for item in (exe, a.binaries, a.datas)],
    strip=False,
    upx=False,
    name=bundle.BUNDLE_NAME,
)
