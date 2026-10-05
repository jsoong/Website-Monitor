from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.logs import configure_logging
from pagewatch.engine.paths import DataDir


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
    }


@pytest.fixture
async def engine(
    data_dir: DataDir, clock: FakeClock, settings_overrides: dict[str, Any]
) -> AsyncIterator[Engine]:
    eng = Engine(data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides)
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


@pytest.fixture
async def api(engine: Engine) -> AsyncIterator[tuple[str, str]]:
    """A real engine API on a random loopback port: (base_url, token)."""
    server = ApiServer(engine)
    port = await server.start()
    try:
        yield f"http://127.0.0.1:{port}", engine.token
    finally:
        await server.stop()


@pytest.fixture
async def client(api: tuple[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    base, token = api
    async with httpx.AsyncClient(
        base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=10
    ) as c:
        yield c
