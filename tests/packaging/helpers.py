"""Shared helpers for the packaging tests.

``packaging/`` is deliberately not a Python package (it would shadow PyPI's ``packaging``), so its
modules are loaded by path, exactly as the spec and ``build.py`` load each other.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"
SRC = ROOT / "src" / "pagewatch"

_loaded: dict[str, ModuleType] = {}


def load_packaging_module(name: str) -> ModuleType:
    """``packaging/<name>.py`` as a module (cached)."""
    if name not in _loaded:
        spec = importlib.util.spec_from_file_location(f"pagewatch_{name}", PACKAGING / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _loaded[name] = module
    return _loaded[name]
