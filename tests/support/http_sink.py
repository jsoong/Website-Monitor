"""A local HTTP endpoint that records what it receives (ntfy, webhooks, asset downloads)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class HttpSink:
    status: int = 200
    reply: bytes = b"ok"
    statuses: list[int] = field(default_factory=list)  # consumed first, one per request
    requests: list[Request] = field(default_factory=list)
    assets: dict[str, tuple[bytes, str]] = field(default_factory=dict)  # path -> (body, type)
    port: int = 0
    _runner: web.AppRunner | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        return self.base + path

    async def start(self) -> HttpSink:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None and site._server.sockets  # type: ignore[attr-defined]
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(Request(request.method, request.path, dict(request.headers), body))
        if request.method == "GET" and request.path in self.assets:
            data, ctype = self.assets[request.path]
            return web.Response(body=data, content_type=ctype)
        status = self.statuses.pop(0) if self.statuses else self.status
        return web.Response(status=status, body=self.reply)
