"""ASGI guard: bearer token, no browser Origin, loopback Host only.

* Every HTTP request needs ``Authorization: Bearer <token>``.
* Any request carrying an ``Origin`` header is refused: browsers add it to cross-site
  requests, so this blocks drive-by calls from web pages (including WebSocket hijacking).
* ``Host`` must be a loopback name, which blocks DNS-rebinding.
* WebSockets cannot set headers from a browser, so the token travels in the first message;
  the WebSocket route checks it. This guard only applies the Origin and Host rules there.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}


def host_ok(host_header: str) -> bool:
    host = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    if host_header.startswith("[") and "]:" in host_header:
        host = host_header.split("]:")[0] + "]"
    return host.lower() in _LOOPBACK


class GuardMiddleware:
    def __init__(self, app: ASGIApp, token: Callable[[], str]) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        problem: tuple[int, str] | None = None
        if b"origin" in headers:
            problem = (403, "browser requests are not allowed")
        elif not host_ok(headers.get(b"host", b"").decode("latin-1")):
            problem = (403, "bad host")
        elif kind == "http":
            auth = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, value = auth.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                value.strip().encode(), self._token().encode()
            ):
                problem = (401, "missing or invalid token")
        if problem is None:
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"detail": problem[1]},
            status_code=problem[0],
            headers={"WWW-Authenticate": "Bearer"} if problem[0] == 401 else None,
        )
        await response(scope, receive, send)
