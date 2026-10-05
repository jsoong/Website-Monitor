"""Worker pool for the CPU-bound half of a check (parse, filter, hash, diff, gate).

The event loop never parses or diffs. It hands raw bytes to a process pool and gets back a
small result; workers read earlier versions from the blob store themselves. ``thread``
mode runs the same code in threads: used by tests (no process start-up cost) and as a
fallback; ``process`` mode is the production default.

A job that runs past ``default_timeout_s`` (a catastrophic user regex, a pathological PDF) is
abandoned: in process mode the workers are killed and the pool is rebuilt, so one bad page can
never wedge every later check; in thread mode (tests) a thread cannot be killed, so it is
abandoned and the executor replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
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


class WorkerTimeout(TimeoutError):
    """A pipeline job ran longer than its time limit and was abandoned."""


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
        self.timeouts = 0
        self.default_timeout_s: float | None = None  # set by the engine from the settings

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

    async def run(self, fn: Callable[..., T], *args: Any, time_limit: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker. A crashed process pool is rebuilt once; a job that
        exceeds ``time_limit`` (default ``default_timeout_s``) raises ``WorkerTimeout`` after its
        worker was killed."""
        loop = asyncio.get_running_loop()
        limit = time_limit if time_limit is not None else self.default_timeout_s
        for attempt in (1, 2):
            executor = self._ensure()
            fut = loop.run_in_executor(executor, fn, *args)
            try:
                if limit is None:
                    result = await fut
                else:
                    done, _ = await asyncio.wait({fut}, timeout=limit)
                    if not done:
                        fut.cancel()
                        self.timeouts += 1
                        log.warning("worker_job_timeout", fn=getattr(fn, "__name__", "?"),
                                    limit_s=limit)  # fmt: skip
                        self._abandon(executor)
                        raise WorkerTimeout(f"processing took longer than {limit:g} s")
                    result = fut.result()
            except BrokenProcessPool:
                log.warning("worker_pool_broken", attempt=attempt)
                self._reset(executor)
                if attempt == 2:
                    raise
                continue
            self.completed += 1
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    def _abandon(self, executor: Executor) -> None:
        """Get rid of an executor holding a job that will not finish: kill its worker
        processes (their state is disposable: blobs are written atomically), then replace it."""
        for proc in list(getattr(executor, "_processes", {}).values()):
            with contextlib.suppress(Exception):
                proc.kill()
        self._reset(executor)

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
