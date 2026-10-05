"""Worker pool for the CPU-bound half of a check (parse, filter, hash, diff, gate).

The event loop never parses or diffs. It hands raw bytes to a process pool and gets back a
small result; workers read earlier versions from the blob store themselves. ``thread``
mode runs the same code in threads: used by tests (no process start-up cost) and as a
fallback; ``process`` mode is the production default.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Literal, TypeVar

from pagewatch.engine.logs import get_logger

T = TypeVar("T")
log = get_logger("pagewatch.workers")

MAX_TASKS_PER_CHILD = 500


def _init_worker() -> None:  # pragma: no cover - runs in the child process
    """Stay out of the user's way, and pay the import cost once per process."""
    try:
        import psutil

        proc = psutil.Process()
        if sys.platform == "win32":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(5)
    except Exception:
        pass


class WorkerPool:
    def __init__(self, processes: int, mode: Literal["process", "thread"] = "process") -> None:
        self.processes = processes
        self.mode = mode
        self._lock = threading.Lock()
        self._executor: Executor | None = None
        self.completed = 0
        self.restarts = 0

    def _ensure(self) -> Executor:
        with self._lock:
            if self._executor is None:
                if self.mode == "process":
                    self._executor = ProcessPoolExecutor(
                        max_workers=self.processes,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=_init_worker,
                        max_tasks_per_child=MAX_TASKS_PER_CHILD,
                    )
                else:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self.processes, thread_name_prefix="pw-worker"
                    )
            return self._executor

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn(*args)`` in a worker. A crashed process pool is rebuilt once."""
        loop = asyncio.get_running_loop()
        for attempt in (1, 2):
            executor = self._ensure()
            try:
                result = await loop.run_in_executor(executor, fn, *args)
            except BrokenProcessPool:
                log.warning("worker_pool_broken", attempt=attempt)
                self._reset(executor)
                if attempt == 2:
                    raise
                continue
            self.completed += 1
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    def _reset(self, broken: Executor) -> None:
        with self._lock:
            if self._executor is broken:
                self._executor = None
                self.restarts += 1
        broken.shutdown(wait=False, cancel_futures=True)

    def shutdown(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
