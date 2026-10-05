import os
from concurrent.futures.process import BrokenProcessPool

import pytest

from pagewatch.engine.store.blobs import sha256_hex
from pagewatch.engine.workers import WorkerPool


async def test_thread_mode_runs_functions() -> None:
    pool = WorkerPool(2, mode="thread")
    try:
        assert await pool.run(sha256_hex, b"abc") == sha256_hex(b"abc")
        assert pool.completed == 1
    finally:
        pool.shutdown()


async def test_process_mode_runs_in_another_process_and_survives_a_crash() -> None:
    pool = WorkerPool(1, mode="process")
    try:
        assert await pool.run(os.getpid) != os.getpid()
        with pytest.raises(BrokenProcessPool):
            await pool.run(os._exit, 1)  # kills the worker twice (original try + retry)
        assert pool.restarts >= 1
        assert await pool.run(sha256_hex, b"abc") == sha256_hex(b"abc")  # pool was rebuilt
    finally:
        pool.shutdown()


# -- the per-job time limit (M5) --------------------------------------------------------------


def _gone(pid: int) -> bool:
    import psutil

    if not psutil.pid_exists(pid):
        return True
    try:
        return bool(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return True


async def test_a_job_past_its_time_limit_kills_the_worker_and_the_pool_keeps_working() -> None:
    import asyncio
    import time

    from pagewatch.engine.workers import WorkerTimeout
    from tests.support import slowjobs

    pool = WorkerPool(1, mode="process")
    try:
        worker = await pool.run(os.getpid)
        started = time.monotonic()
        with pytest.raises(WorkerTimeout, match="longer than 1 s"):
            await pool.run(slowjobs.spin, 120, time_limit=1.0)
        assert time.monotonic() - started < 15  # it did not wait for the job
        assert pool.timeouts == 1 and pool.restarts >= 1
        for _ in range(100):  # the busy worker process was killed, not left spinning
            if _gone(worker):
                break
            await asyncio.sleep(0.1)
        assert _gone(worker)
        assert await pool.run(slowjobs.quick, 21) == 42  # and a fresh pool serves the next job
    finally:
        pool.shutdown()


async def test_the_default_limit_applies_to_every_job_and_none_means_unlimited() -> None:
    from pagewatch.engine.workers import WorkerTimeout
    from tests.support import slowjobs

    pool = WorkerPool(1, mode="process")
    try:
        pool.default_timeout_s = 0.5
        with pytest.raises(WorkerTimeout):
            await pool.run(slowjobs.spin, 120)
        pool.default_timeout_s = None
        assert await pool.run(slowjobs.spin, 0.3) > 0  # no limit: it just runs
    finally:
        pool.shutdown()


async def test_a_timeout_inside_the_job_is_not_mistaken_for_ours() -> None:
    """A job that itself raises ``TimeoutError`` propagates it unchanged."""
    pool = WorkerPool(1, mode="thread")
    try:
        pool.default_timeout_s = 5.0

        def raises() -> None:
            raise TimeoutError("the site's own timeout")

        from pagewatch.engine.workers import WorkerTimeout

        with pytest.raises(TimeoutError, match="the site's own") as info:
            await pool.run(raises)
        assert not isinstance(info.value, WorkerTimeout) and pool.timeouts == 0
    finally:
        pool.shutdown()
