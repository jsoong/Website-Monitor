"""Run the API on 127.0.0.1 with a random port inside the engine's event loop."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Iterator

import uvicorn

from pagewatch.engine.api.app import create_app
from pagewatch.engine.core import Engine


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The engine owns shutdown (Quit, Ctrl+C, WM_QUERYENDSESSION); uvicorn must not
        # install its own handlers or re-raise the signal after serving.
        yield


class ApiServer:
    def __init__(self, engine: Engine, host: str = "127.0.0.1") -> None:
        self.engine = engine
        self.host = host
        self.port = 0
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        sock.listen(128)
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            create_app(self.engine),
            log_config=None,
            lifespan="off",
            access_log=False,
            ws="websockets-sansio",
        )
        self._server = _Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]), name="api-server")
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise RuntimeError("API server exited during start-up")
            await asyncio.sleep(0.01)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10)
