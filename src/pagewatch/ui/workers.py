"""Run blocking API calls off the GUI thread and deliver the result *on* it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Dispatcher(QObject):
    """Lives in the GUI thread. A signal emitted from a worker thread reaches ``_invoke``
    through a queued connection, so callbacks always run on the GUI thread."""

    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._invoke)

    @Slot(object)
    def _invoke(self, fn: Callable[[], None]) -> None:
        fn()


_dispatcher: _Dispatcher | None = None
_pool: QThreadPool | None = None


def _get() -> tuple[_Dispatcher, QThreadPool]:
    global _dispatcher, _pool
    if _dispatcher is None:
        _dispatcher = _Dispatcher()  # must be created in the GUI thread
        _pool = QThreadPool()
        _pool.setMaxThreadCount(6)
    assert _pool is not None
    return _dispatcher, _pool


class _Task(QRunnable):
    def __init__(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None] | None,
        on_error: Callable[[Exception], None] | None,
        dispatcher: _Dispatcher,
    ) -> None:
        super().__init__()
        self.fn, self.on_done, self.on_error, self.dispatcher = fn, on_done, on_error, dispatcher

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:
            if self.on_error is not None:
                cb = self.on_error
                self.dispatcher.call.emit(lambda err=exc: cb(err))  # `exc` dies with this block
            return
        if self.on_done is not None:
            done = self.on_done
            self.dispatcher.call.emit(lambda: done(result))


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    dispatcher, pool = _get()
    pool.start(_Task(fn, on_done, on_error, dispatcher))


def wait_idle(timeout_ms: int = 5000) -> bool:
    """Block (processing events) until every started task finished; for tests and shutdown."""
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    _, pool = _get()
    clock = QElapsedTimer()
    clock.start()
    while pool.activeThreadCount() and clock.elapsed() < timeout_ms:
        QCoreApplication.processEvents()
    QCoreApplication.processEvents()
    return pool.activeThreadCount() == 0
