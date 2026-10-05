"""An in-process FTP server (aioftp) serving a directory: anonymous plus alice / s3cret."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aioftp


@dataclass
class Ftp:
    root: Path
    port: int

    def url(self, path: str = "/") -> str:
        return f"ftp://127.0.0.1:{self.port}{path}"


@asynccontextmanager
async def serve(root: Path) -> AsyncIterator[Ftp]:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    perms = [aioftp.Permission("/", readable=True)]
    users = [
        aioftp.User(base_path=root, permissions=perms),  # anonymous
        aioftp.User("alice", "s3cret", base_path=root, permissions=perms),
    ]
    server = aioftp.Server(users)
    await server.start("127.0.0.1", 0)
    port = server.server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield Ftp(root, port)
    finally:
        await server.close()
