from __future__ import annotations

import os
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.instance import remove_lockfile, write_lockfile
from pagewatch.engine.logs import configure_logging
from pagewatch.engine.paths import DataDir
from pagewatch.models import LockInfo
from tests.support.fixture_site import FixtureSite


@pytest.fixture(autouse=True, scope="session")
def _logging() -> None:
    configure_logging(None, console=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> DataDir:
    return DataDir(tmp_path / "data").ensure()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """Fast, polite-to-localhost defaults for tests; individual tests can shadow this."""
    return {
        "startup_delay_s": 0.0,
        "per_host_min_gap_s": 0.0,
        "per_host_concurrency": 16,
        "worker_processes": 2,
        "toast_coalesce_s": 0.0,  # immediate toasts; coalescing tests set it explicitly
    }


@pytest.fixture
def toasts() -> LogToastBackend:
    return LogToastBackend()


@pytest.fixture
async def engine(
    data_dir: DataDir,
    clock: FakeClock,
    settings_overrides: dict[str, Any],
    toasts: LogToastBackend,
) -> AsyncIterator[Engine]:
    eng = Engine(
        data_dir,
        clock=clock,
        worker_mode="thread",
        settings_overrides=settings_overrides,
        toast_backend=toasts,
        rng=random.Random(20260105),  # deterministic jitter
    )
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


@pytest.fixture
async def site() -> AsyncIterator[FixtureSite]:
    s = await FixtureSite().start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.fixture
async def api(engine: Engine, data_dir: DataDir) -> AsyncIterator[tuple[str, str]]:
    """A real engine API on a random loopback port: (base_url, token). Also writes
    engine.lock so the CLI can discover it, exactly like the real engine does."""
    server = ApiServer(engine)
    port = await server.start()
    write_lockfile(
        data_dir,
        LockInfo(pid=os.getpid(), port=port, token=engine.token, version="test", started_at="now"),
    )
    try:
        yield f"http://127.0.0.1:{port}", engine.token
    finally:
        await server.stop()
        remove_lockfile(data_dir)


@pytest.fixture
async def client(api: tuple[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    base, token = api
    async with httpx.AsyncClient(
        base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=10
    ) as c:
        yield c
