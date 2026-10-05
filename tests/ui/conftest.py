from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

# Qt must see these before it is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pagewatch.ui.client import ApiClient  # noqa: E402
from pagewatch.ui.workers import wait_idle  # noqa: E402
from tests.support.engine_thread import EngineThread  # noqa: E402


def _teardown_qt(app: QApplication) -> None:
    """QtWebEngine must see every view and page destroyed *before* the QApplication: left to
    Python's garbage collector at interpreter exit the order is arbitrary and the process
    segfaults after all tests passed. Close and delete everything deterministically."""
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool

    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    for _ in range(5):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        gc.collect()
    QThreadPool.globalInstance().waitForDone(2000)


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication(["pagewatch-tests"])
    yield app  # type: ignore[misc]
    _teardown_qt(app)


def pump(cond: Callable[[], bool], timeout: float = 15.0, what: str = "condition") -> None:
    """Process Qt events until ``cond()`` holds (the GUI never blocks the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def pump_until() -> Callable[..., None]:
    return pump


@pytest.fixture
def eng(tmp_path: Path, qapp: QApplication) -> Iterator[EngineThread]:
    et = EngineThread(tmp_path / "data").start()
    try:
        yield et
    finally:
        wait_idle(2000)
        et.stop()


@pytest.fixture
def api_client(eng: EngineThread) -> Iterator[ApiClient]:
    c = ApiClient(eng.base, eng.engine.token)
    yield c
    c.close()
