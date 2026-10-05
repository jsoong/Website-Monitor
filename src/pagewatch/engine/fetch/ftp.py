"""FTP and FTPS (``ftp://`` and ``ftps://``): files and directory listings, via aioftp.

A file is downloaded (capped at ``max_bytes``) and goes through the document or text path; an
unchanged size + modified time answers ``not_modified`` without downloading it. A directory
becomes a listing table of name, size and modified time. ``ftps://`` is explicit FTPS (AUTH TLS)
on the given port, or implicit TLS on port 990. The password never lives in the database: the
bookmark names a secret and the ``SecretStore`` supplies the value.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import aioftp

from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.fetch.localfile import content_type_for, listing_html
from pagewatch.engine.fetch.static import classify
from pagewatch.engine.secrets import NullSecrets, SecretStore
from pagewatch.models import FetchErrorKind

ANON_PASSWORD = "anonymous@"
IMPLICIT_TLS_PORT = 990


def _modify(info: Mapping[str, Any]) -> datetime | None:
    raw = str(info.get("modify") or "")
    for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _status_error(exc: aioftp.StatusCodeError) -> FetchError:
    code = str(exc.received_codes[-1]) if exc.received_codes else ""
    message = f"FTP {code or 'error'}: {exc.info}"[:300] if exc.info else f"FTP {code}"
    if code.startswith("5"):
        if code in ("530", "532"):
            return FetchError(FetchErrorKind.HTTP, message, status=401)
        if code in ("550", "553"):
            return FetchError(FetchErrorKind.HTTP, message, status=404)
        return FetchError(FetchErrorKind.HTTP, message, status=502)
    return FetchError(FetchErrorKind.CONNECTION, message)  # 4xx: try again later


class FtpFetcher:
    def __init__(self, secrets: SecretStore | None = None) -> None:
        self._secrets: SecretStore = secrets or NullSecrets()

    async def aclose(self) -> None:
        return None

    async def fetch(self, request: FetchRequest) -> FetchResult:
        started = time.perf_counter()
        cfg = request.resolved.fetch

        def done(res: FetchResult) -> FetchResult:
            res.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return res

        def fail(err: FetchError) -> FetchResult:
            return done(FetchResult(request.url, None, {}, "", b"", error=err))

        parts = urlsplit(request.url)
        if not parts.hostname:
            return fail(FetchError(FetchErrorKind.PARSE, f"bad url: {request.url}"))
        user = cfg.auth.username if cfg.auth else (parts.username or "anonymous")
        password = ANON_PASSWORD
        if cfg.auth:
            secret = self._secrets.get(cfg.auth.secret_key)
            if secret is None:
                return fail(FetchError(
                    FetchErrorKind.HTTP, f"no stored secret named {cfg.auth.secret_key!r}",
                    status=401,
                ))  # fmt: skip
            password = secret
        secure = parts.scheme.lower() == "ftps"
        port = parts.port or 21
        implicit = secure and port == IMPLICIT_TLS_PORT
        context: ssl.SSLContext | None = None
        if secure:
            context = ssl.create_default_context()
            if not cfg.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        path = unquote(parts.path) or "/"
        client = aioftp.Client(
            socket_timeout=cfg.timeout_s,
            connection_timeout=cfg.timeout_s,
            ssl=context if implicit else None,
        )
        try:
            async with asyncio.timeout(cfg.timeout_s * 4 + 5):
                await client.connect(parts.hostname, port)
                if secure and not implicit:
                    await client.upgrade_to_tls(context)
                await client.login(user, password)
                info = await client.stat(path)
                if info.get("type") == "dir":
                    return done(await self._listing(client, request, path))
                return done(await self._download(client, request, path, info))
        except aioftp.StatusCodeError as exc:
            return fail(_status_error(exc))
        except Exception as exc:
            return fail(classify(exc))
        finally:
            client.close()

    async def _listing(
        self, client: aioftp.Client, request: FetchRequest, path: str
    ) -> FetchResult:
        opts = request.resolved.fetch.listing
        base = PurePosixPath(path)
        rows: list[tuple[str, int | None, float | None]] = []
        async for entry, info in client.list(path, recursive=opts.recursive):
            is_dir = info.get("type") == "dir"
            try:
                rel = entry.relative_to(base).as_posix()
            except ValueError:
                rel = entry.name
            when = _modify(info)
            size = info.get("size")
            rows.append((
                rel + ("/" if is_dir else ""),
                None if is_dir or size is None else int(size),
                when.timestamp() if when else None,
            ))  # fmt: skip
            if len(rows) > opts.max_entries:
                return FetchResult(
                    request.url, None, {}, "", b"",
                    error=FetchError(
                        FetchErrorKind.TOO_LARGE,
                        f"more than {opts.max_entries} entries; raise listing.max_entries",
                    ),
                )  # fmt: skip
        return FetchResult(
            request.url, 200, {}, "text/html; charset=utf-8", listing_html(rows).encode()
        )

    async def _download(
        self, client: aioftp.Client, request: FetchRequest, path: str, info: Mapping[str, Any]
    ) -> FetchResult:
        cfg = request.resolved.fetch
        size = info.get("size")
        when = _modify(info)
        headers: dict[str, str] = {}
        sig = f"{size}-{info.get('modify')}" if size is not None and info.get("modify") else None
        if sig:
            headers["etag"] = sig
        if when:
            headers["last-modified"] = format_datetime(when, usegmt=True)
        ctype = content_type_for(path)
        if sig and not request.force and request.etag == sig:
            return FetchResult(request.url, 200, headers, ctype, b"", not_modified=True)
        if size is not None and int(size) > cfg.max_bytes:
            return FetchResult(
                request.url, None, {}, "", b"",
                error=FetchError(FetchErrorKind.TOO_LARGE, f"{size} bytes > {cfg.max_bytes}"),
            )  # fmt: skip
        chunks: list[bytes] = []
        total = 0
        async with client.download_stream(path) as stream:
            async for block in stream.iter_by_block():
                total += len(block)
                if total > cfg.max_bytes:
                    return FetchResult(
                        request.url, None, {}, "", b"",
                        error=FetchError(
                            FetchErrorKind.TOO_LARGE, f"body exceeds {cfg.max_bytes} bytes"
                        ),
                    )  # fmt: skip
                chunks.append(block)
        return FetchResult(request.url, 200, headers, ctype, b"".join(chunks))
