"""A local web server whose pages tests change on demand."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from aiohttp import web


@dataclass
class PageDef:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    etag: bool = False
    generator: Callable[[int], str] | None = None  # body from the hit number
    calls: int = 0


@dataclass
class Hit:
    method: str
    path: str
    query: str
    status: int
    headers: dict[str, str]


class FixtureSite:
    def __init__(self) -> None:
        self.pages: dict[str, PageDef] = {}
        self.hits: list[Hit] = []
        self._runner: web.AppRunner | None = None
        self.port = 0
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        return self.base + path

    def set(self, path: str, body: str | bytes, **kw: object) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.pages[path] = PageDef(data, **kw)  # type: ignore[arg-type]

    def set_dynamic(self, path: str, fn: Callable[[int], str], **kw: object) -> None:
        """A page whose bytes are computed per request (e.g. an embedded timestamp)."""
        self.pages[path] = PageDef(b"", generator=fn, **kw)  # type: ignore[arg-type]

    def hits_for(self, path: str) -> list[Hit]:
        return [h for h in self.hits if h.path == path]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        page = self.pages.get(request.path)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if page is not None and page.delay:
                await asyncio.sleep(page.delay)
            if page is None:
                self.hits.append(
                    Hit(
                        request.method,
                        request.path,
                        request.query_string,
                        404,
                        dict(request.headers),
                    )
                )
                return web.Response(status=404, text="not found")
            headers = dict(page.headers)
            if page.generator is not None:
                page.calls += 1
                page.body = page.generator(page.calls).encode()
            if page.etag:
                tag = '"' + hashlib.md5(page.body).hexdigest() + '"'
                headers["ETag"] = tag
                if request.headers.get("If-None-Match") == tag:
                    self.hits.append(
                        Hit(
                            request.method,
                            request.path,
                            request.query_string,
                            304,
                            dict(request.headers),
                        )
                    )
                    return web.Response(status=304, headers=headers)
            self.hits.append(
                Hit(
                    request.method,
                    request.path,
                    request.query_string,
                    page.status,
                    dict(request.headers),
                )
            )
            return web.Response(
                status=page.status, body=page.body, content_type=page.content_type.split(";")[0],
                charset="utf-8" if "charset" in page.content_type else None, headers=headers,
            )  # fmt: skip
        finally:
            self.in_flight -= 1

    async def start(self) -> FixtureSite:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert self._runner.addresses
        self.port = self._runner.addresses[0][1]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


def article(*items: str, title: str = "News") -> str:
    """A small realistic page: heading, list of items, footer."""
    lis = "".join(f"<li>{i}</li>" for i in items)
    return (
        f"<html><head><title>{title}</title></head><body><h1>{title}</h1>"
        f"<ul>{lis}</ul><footer>Contact us</footer></body></html>"
    )
