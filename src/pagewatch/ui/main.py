"""``pagewatch-ui``: the desktop client. It can open and close freely; the engine keeps
monitoring either way."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence

from pagewatch import __version__
from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir, resolve_data_dir


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pagewatch-ui", description="PageWatch desktop UI")
    p.add_argument("--data-dir", help="data folder of the engine to connect to")
    p.add_argument("--version", action="version", version=f"pagewatch-ui {__version__}")
    return p


def start_engine(data_dir: DataDir, wait_s: float = 25.0) -> bool:
    """Launch a detached engine for this data folder and wait for its lockfile."""
    args = [
        sys.executable,
        "-m",
        "pagewatch.engine.main",
        "--data-dir",
        str(data_dir.root),
        "--no-console-log",
    ]
    flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
    subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,  # noqa: S603
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if read_lockfile(data_dir) is not None:
            return True
        time.sleep(0.2)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from PySide6.QtWidgets import QApplication, QMessageBox

    from pagewatch.ui import theme
    from pagewatch.ui.client import EngineNotRunning, connect
    from pagewatch.ui.events import EventStream
    from pagewatch.ui.windows.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("PageWatch")
    theme.follow_system()
    data_dir = resolve_data_dir(args.data_dir)
    try:
        client = connect(data_dir)
    except EngineNotRunning:
        answer = QMessageBox.question(
            None,
            "PageWatch",
            "The PageWatch engine is not running, so nothing is being monitored.\n\nStart it now?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return 1
        data_dir.ensure()
        if not start_engine(data_dir):
            QMessageBox.critical(
                None, "PageWatch", "The engine did not start. See logs\\engine.log."
            )
            return 1
        client = connect(data_dir)
    events = EventStream(client.ws_url, client.token)
    window = MainWindow(client, events, persist=True)
    events.start()
    window.show()
    code = app.exec()
    client.close()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
