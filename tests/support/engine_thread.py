"""An engine (fake clock, fixture web server, real HTTP API) running in a background thread with
its own event loop, so a Qt GUI in the main thread can talk to it over real HTTP/WebSocket."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.instance import remove_lockfile, write_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.models import LockInfo
from tests.support.fixture_site import FixtureSite
from tests.support.sim import advance, settle

T = TypeVar("T")


class EngineThread:
    def __init__(self, root: Path, overrides: dict[str, Any] | None = None) -> None:
        self.data_dir = DataDir(root).ensure()
        self.overrides = {
            "startup_delay_s": 0.0, "per_host_min_gap_s": 0.0, "per_host_concurrency": 16,
            "worker_processes": 2, "toast_coalesce_s": 0.0, **(overrides or {}),
        }  # fmt: skip
        self.clock = FakeClock()
        self.toasts = LogToastBackend()
        self.site = FixtureSite()
        self.loop = asyncio.new_event_loop()
        self.engine: Engine
        self.server: ApiServer
        self.port = 0
        self._thread = threading.Thread(target=self._run, name="engine-thread", daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def boot() -> None:
            await self.site.start()
            self.engine = Engine(
                self.data_dir, clock=self.clock, worker_mode="thread",
                settings_overrides=self.overrides, toast_backend=self.toasts,
            )  # fmt: skip
            await self.engine.start()
            self.server = ApiServer(self.engine)
            self.port = await self.server.start()
            write_lockfile(
                self.data_dir,
                LockInfo(
                    pid=os.getpid(),
                    port=self.port,
                    token=self.engine.token,
                    version="test",
                    started_at="now",
                ),
            )

        self.loop.run_until_complete(boot())
        self._ready.set()
        self.loop.run_forever()

    def start(self) -> EngineThread:
        self._thread.start()
        assert self._ready.wait(30), "engine thread did not start"
        return self

    def call(self, coro: Coroutine[Any, Any, T], timeout: float = 60) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def advance(self, seconds: float, step: float = 10.0) -> None:
        self.call(advance(self.engine, self.clock, seconds, step))

    def settle(self) -> None:
        self.call(settle(self.engine, self.clock))

    def stop(self) -> None:
        async def down() -> None:
            await self.server.stop()
            await self.engine.stop()
            await self.site.stop()
            remove_lockfile(self.data_dir)

        self.call(down())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"
