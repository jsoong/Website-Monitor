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
