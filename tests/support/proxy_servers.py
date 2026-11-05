"""Tiny proxies for tests: an HTTP forward proxy and a SOCKS5 server.

Both answer for *virtual* host names (``proxied.test``), which exist nowhere else, so a page
that arrives proves the request really went through the proxy. ``upstream`` maps a virtual
host name to the real ``(host, port)`` of a fixture server.
"""

from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


@dataclass
class ProxySeen:
    host: str
    path: str
    proxy_authorization: str | None = None


@dataclass
class HttpProxy:
    upstream: dict[str, tuple[str, int]]
    credentials: tuple[str, str] | None = None
    seen: list[ProxySeen] = field(default_factory=list)
    port: int = 0

    def __post_init__(self) -> None:
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        target = urlsplit(str(request.url))
        auth = request.headers.get("Proxy-Authorization")
        self.seen.append(ProxySeen(target.hostname or "", request.path_qs, auth))
        if self.credentials is not None:
            expected = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
            if auth != expected:
                return web.Response(
                    status=407, headers={"Proxy-Authenticate": 'Basic realm="proxy"'}
                )
        where = self.upstream.get(target.hostname or "")
        if where is None:
            return web.Response(status=502, text="unknown host")
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("proxy-authorization", "host")
        }
        async with (
            aiohttp.ClientSession() as session,
            session.request(
                request.method,
                f"http://{where[0]}:{where[1]}{request.path_qs}",
                headers=headers,
                allow_redirects=False,
            ) as upstream,
        ):
            body = await upstream.read()
            return web.Response(
                status=upstream.status,
                body=body,
                headers={
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower() in ("content-type", "location", "set-cookie")
                },
            )

    async def start(self) -> HttpProxy:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        assert self._runner.addresses
        self.port = self._runner.addresses[0][1]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@dataclass
class Socks5Proxy:
    upstream: dict[str, tuple[str, int]]
    credentials: tuple[str, str] | None = None
    requested: list[str] = field(default_factory=list)
    port: int = 0

    def __post_init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            _ver, count = struct.unpack("!BB", await reader.readexactly(2))
            methods = await reader.readexactly(count)
            if self.credentials is not None:
                if 2 not in methods:
                    writer.write(b"\x05\xff")
                    return
                writer.write(b"\x05\x02")
                await reader.readexactly(1)
                user = await reader.readexactly((await reader.readexactly(1))[0])
                pw = await reader.readexactly((await reader.readexactly(1))[0])
                ok = (user.decode(), pw.decode()) == self.credentials
                writer.write(b"\x01\x00" if ok else b"\x01\x01")
                if not ok:
                    return
            else:
                writer.write(b"\x05\x00")
            await writer.drain()
            _v, _cmd, _rsv, atyp = struct.unpack("!BBBB", await reader.readexactly(4))
            if atyp == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            elif atyp == 1:
                host = ".".join(str(b) for b in await reader.readexactly(4))
            else:
                writer.write(b"\x05\x08\x00\x01" + bytes(6))
                return
            (port,) = struct.unpack("!H", await reader.readexactly(2))
            self.requested.append(host)
            where = self.upstream.get(host, (host, port))
            try:
                up_reader, up_writer = await asyncio.open_connection(*where)
            except OSError:
                writer.write(b"\x05\x04\x00\x01" + bytes(6))
                return
            writer.write(b"\x05\x00\x00\x01" + bytes(6))
            await writer.drain()

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                try:
                    while data := await src.read(65536):
                        dst.write(data)
                        await dst.drain()
                except (ConnectionError, asyncio.IncompleteReadError):
                    pass
                finally:
                    dst.close()

            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def start(self) -> Socks5Proxy:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
