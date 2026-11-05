"""A tiny SMTP server for tests: plain TCP (no TLS), AUTH PLAIN/LOGIN, and failure modes.

Enough of RFC 5321 for ``aiosmtplib`` to deliver a message: EHLO/HELO, AUTH, MAIL, RCPT, DATA,
RSET, NOOP, QUIT. Everything delivered is kept in ``messages``.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    data: bytes
    auth: tuple[str, str] | None = None

    @property
    def message(self) -> Message:
        return message_from_bytes(self.data)

    def text(self) -> str:
        """The text/plain part, decoded."""
        msg = self.message
        part = (
            msg
            if not msg.is_multipart()
            else next((p for p in msg.walk() if p.get_content_type() == "text/plain"), msg)
        )
        payload = part.get_payload(decode=True)
        text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        return text.replace("\r\n", "\n")  # SMTP carries CRLF

    def html(self) -> str | None:
        for p in self.message.walk():
            if p.get_content_type() == "text/html":
                payload = p.get_payload(decode=True)
                assert isinstance(payload, bytes)
                return payload.decode("utf-8").replace("\r\n", "\n")
        return None


@dataclass
class SmtpServer:
    require_auth: tuple[str, str] | None = None  # (user, password) the server accepts
    reject: str | None = None  # e.g. "554 no" answered to MAIL FROM (permanent failure)
    drop_first: int = 0  # close the connection on the first N sessions
    messages: list[Received] = field(default_factory=list)
    sessions: int = 0
    port: int = 0
    _server: asyncio.Server | None = None

    async def start(self) -> SmtpServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _send(self, w: asyncio.StreamWriter, line: str) -> None:
        w.write(line.encode() + b"\r\n")
        await w.drain()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.sessions += 1
        try:
            if self.sessions <= self.drop_first:
                w.close()
                return
            await self._send(w, "220 test ESMTP")
            mail_from, rcpt, auth = "", [], None
            while line := await r.readline():
                cmd = line.decode(errors="replace").strip()
                verb = cmd.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    await self._send(w, "250-test")
                    if self.require_auth:
                        await self._send(w, "250-AUTH PLAIN LOGIN")
                    await self._send(w, "250 8BITMIME")
                elif verb == "AUTH":
                    auth = await self._auth(r, w, cmd)
                elif verb == "MAIL":
                    if self.reject:
                        await self._send(w, self.reject)
                        continue
                    if self.require_auth and auth != self.require_auth:
                        await self._send(w, "530 authentication required")
                        continue
                    mail_from = cmd.split(":", 1)[1].strip().strip("<>").split(">")[0]
                    rcpt = []
                    await self._send(w, "250 ok")
                elif verb == "RCPT":
                    rcpt.append(cmd.split(":", 1)[1].strip().strip("<>").split(">")[0])
                    await self._send(w, "250 ok")
                elif verb == "DATA":
                    await self._send(w, "354 go on")
                    chunks: list[bytes] = []
                    while True:
                        part = await r.readline()
                        if part in (b".\r\n", b""):
                            break
                        chunks.append(part[1:] if part.startswith(b"..") else part)
                    self.messages.append(Received(mail_from, rcpt, b"".join(chunks), auth))
                    await self._send(w, "250 queued")
                elif verb in ("RSET", "NOOP"):
                    await self._send(w, "250 ok")
                elif verb == "QUIT":
                    await self._send(w, "221 bye")
                    break
                else:
                    await self._send(w, "502 not implemented")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()

    async def _auth(
        self, r: asyncio.StreamReader, w: asyncio.StreamWriter, cmd: str
    ) -> tuple[str, str] | None:
        parts = cmd.split()
        if len(parts) >= 2 and parts[1].upper() == "PLAIN":
            blob = parts[2] if len(parts) > 2 else None
            if blob is None:
                await self._send(w, "334 ")
                blob = (await r.readline()).decode().strip()
            _, user, password = base64.b64decode(blob).decode().split("\x00")
        elif len(parts) >= 2 and parts[1].upper() == "LOGIN":
            await self._send(w, "334 " + base64.b64encode(b"Username:").decode())
            user = base64.b64decode((await r.readline()).strip()).decode()
            await self._send(w, "334 " + base64.b64encode(b"Password:").decode())
            password = base64.b64decode((await r.readline()).strip()).decode()
        else:
            await self._send(w, "504 unsupported")
            return None
        if self.require_auth == (user, password):
            await self._send(w, "235 ok")
            return (user, password)
        await self._send(w, "535 bad credentials")
        return None
