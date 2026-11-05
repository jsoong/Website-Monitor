"""Running from source versus from a PyInstaller bundle.

Several places start another PageWatch program as a child process: the UI starts the engine, the
engine's tray opens the UI, the browser manager installs Playwright's Chromium (and, from M5, the
engine restarts itself and the Task Scheduler task names the engine). From source
that is ``python -m <module>``. In the packaged build there is no ``python``: ``sys.executable`` is
the program that is running and its two siblings sit next to it, so a command assembled from
``sys.executable`` would start the wrong thing (or, for ``-m``, nothing).

Everything that differs between the two worlds lives here so the rest of the code does not test
``sys.frozen``:

* :func:`component_command` - the command line that starts the engine, the UI or the CLI;
* :func:`playwright_install_command` - how to run Playwright's installer;
* :func:`launch` - what every packaged entry point does first (``freeze_support``, streams);
* :func:`prepare_streams` - ``sys.stdout``/``sys.stderr`` are ``None`` in a windowed Windows build.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NamedTuple, TextIO, cast

Component = Literal["engine", "ui", "cli"]

#: The module whose ``main()`` each program runs, and the name of its executable (without
#: ``.exe``). ``pyproject.toml`` ``[project.scripts]`` names the same three programs.
MODULES: dict[Component, str] = {
    "engine": "pagewatch.engine.main",
    "ui": "pagewatch.ui.main",
    "cli": "pagewatch.cli.main",
}
PROGRAMS: dict[Component, str] = {
    "engine": "pagewatch-engine",
    "ui": "pagewatch-ui",
    "cli": "pagewatch-cli",
}

ATTACH_PARENT_PROCESS = 0xFFFFFFFF


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The folder that holds the packaged programs (the folder of the running executable)."""
    return Path(sys.executable).resolve().parent


def program_path(component: Component) -> Path:
    """Where a packaged program lives, next to the running one."""
    suffix = ".exe" if sys.platform == "win32" else ""
    return app_dir() / f"{PROGRAMS[component]}{suffix}"


def component_command(component: Component, *args: str) -> list[str]:
    """The command line that starts ``component`` with ``args``.

    From source: ``[python, "-m", module, *args]``. Packaged: ``[<app dir>/<program>, *args]``.
    """
    if is_frozen():
        return [str(program_path(component)), *args]
    return [sys.executable, "-m", MODULES[component], *args]


class InstallCommand(NamedTuple):
    argv: list[str]
    env: dict[str, str]


def playwright_install_command(browser: str = "chromium") -> InstallCommand:
    """``playwright install <browser>`` without needing ``python -m playwright``.

    Playwright for Python is a thin client of a Node program it ships (``playwright/driver``);
    ``python -m playwright`` only forwards its arguments to that program. Calling the program
    directly is the same thing from source and works in a bundle, where there is no Python to run
    ``-m`` with (the build collects ``playwright/driver``).
    """
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli = compute_driver_executable()
    return InstallCommand([node, cli, "install", browser], dict(get_driver_env()))


def _attach_parent_console() -> bool:
    """Reconnect a windowed Windows program to the console it was started from, if any, so
    ``pagewatch-engine --version`` and error messages still show in a terminal."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS))
    except Exception:
        return False


def _open_console() -> TextIO:
    return open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115


def _open_null(mode: str) -> TextIO:
    # lives for the process; a variable mode types as IO[Any], and this is always a text file
    return cast("TextIO", open(os.devnull, mode, encoding="utf-8"))  # noqa: SIM115


def prepare_streams() -> None:
    """Give ``sys.stdin``/``stdout``/``stderr`` a value when the program has no console.

    A PyInstaller program built with ``console=False`` starts with all three set to ``None``, and
    code that does ``print(..., file=sys.stderr)`` or hands ``sys.stderr`` to a logging handler
    then misbehaves. They become the parent's console when there is one (started from a terminal)
    and the null device otherwise (started by Task Scheduler or the installer).
    """
    missing = [n for n in ("stdin", "stdout", "stderr") if getattr(sys, n) is None]
    if not missing:
        return
    console = _attach_parent_console()
    for name in missing:
        if name == "stdin":
            sys.stdin = _open_null("r")
            continue
        stream: TextIO | None = None
        if console:
            try:
                stream = _open_console()
            except OSError:
                stream = None
        setattr(sys, name, stream if stream is not None else _open_null("w"))


def launch(component: Component, argv: Sequence[str] | None = None) -> int:
    """What a packaged executable does first, then runs the component's ``main``.

    ``multiprocessing.freeze_support()`` has to come before anything else: the worker pool uses the
    ``spawn`` start method, and in a bundle each worker is a fresh copy of this very executable
    started with ``--multiprocessing-fork``. ``freeze_support`` recognises that and runs the worker
    (never returning); without it every worker would start the whole application again. The
    component's module is imported only after it, so a worker does not pay for importing the API
    server or the UI.
    """
    multiprocessing.freeze_support()
    prepare_streams()
    module = importlib.import_module(MODULES[component])
    return int(module.main(argv))
