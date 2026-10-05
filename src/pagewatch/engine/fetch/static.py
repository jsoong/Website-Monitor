"""Static HTTP(S) fetcher: httpx, HTTP/2, conditional GET, hard body cap."""

from __future__ import annotations

import asyncio
import email.utils
import socket
import ssl
import time
from datetime import UTC, datetime

import httpx

from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.models import FetchErrorKind

MAX_REDIRECTS = 10
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7"


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """``Retry-After`` is delay-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def classify(exc: BaseException) -> FetchError:
    """Map an httpx/socket/ssl exception to a ``FetchError``."""
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    text = " | ".join(str(e) for e in chain)
    if isinstance(exc, httpx.TimeoutException | TimeoutError | asyncio.TimeoutError):
        return FetchError(FetchErrorKind.TIMEOUT, "timed out")
    if any(isinstance(e, ssl.SSLError) for e in chain) or "CERTIFICATE_VERIFY_FAILED" in text:
        return FetchError(FetchErrorKind.TLS, text[:300])
    if any(isinstance(e, socket.gaierror) for e in chain) or "Name or service not known" in text:
        return FetchError(FetchErrorKind.DNS, text[:300])
    if isinstance(exc, httpx.TooManyRedirects):
        return FetchError(FetchErrorKind.HTTP, "too many redirects")
    if isinstance(exc, httpx.InvalidURL | httpx.UnsupportedProtocol):
        return FetchError(FetchErrorKind.PARSE, f"bad url: {exc}")
    if isinstance(exc, httpx.DecodingError):
        return FetchError(FetchErrorKind.PARSE, str(exc)[:300])
    if isinstance(exc, httpx.TransportError | OSError):
        return FetchError(FetchErrorKind.CONNECTION, text[:300])
    return FetchError(FetchErrorKind.CONNECTION, f"{type(exc).__name__}: {exc}"[:300])


class StaticFetcher:
    def __init__(self) -> None:
        self._clients: dict[tuple[str | None, bool], httpx.AsyncClient] = {}

    def client_for(self, proxy: str | None, verify: bool) -> httpx.AsyncClient:
        """The pooled client for these transport settings (also used for enclosure downloads)."""
        key = (proxy, verify)
        client = self._clients.get(key)
        if client is None:
            client = httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                verify=verify,
                proxy=proxy,
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
                timeout=httpx.Timeout(30.0),
            )
            self._clients[key] = client
        return client

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for c in clients:
            await c.aclose()

    async def fetch(self, request: FetchRequest) -> FetchResult:
        cfg = request.resolved.fetch
        started = time.perf_counter()
        headers = {
            "User-Agent": cfg.user_agent or request.settings.default_user_agent,
            "Accept": ACCEPT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        headers.update(cfg.headers)
        if not request.force:
            if request.etag:
                headers.setdefault("If-None-Match", request.etag)
            if request.last_modified:
                headers.setdefault("If-Modified-Since", request.last_modified)
        proxy = cfg.proxy or request.settings.global_proxy
        client = self.client_for(proxy, cfg.verify_tls)

        def result(
            resp: httpx.Response | None,
            body: bytes = b"",
            error: FetchError | None = None,
            not_modified: bool = False,
        ) -> FetchResult:
            elapsed = int((time.perf_counter() - started) * 1000)
            if resp is None:
                return FetchResult(request.url, None, {}, "", b"", elapsed_ms=elapsed, error=error)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return FetchResult(
                final_url=str(resp.url),
                status=resp.status_code,
                headers=hdrs,
                content_type=hdrs.get("content-type", ""),
                body=body,
                elapsed_ms=elapsed,
                not_modified=not_modified,
                error=error,
            )

        try:
            async with asyncio.timeout(cfg.timeout_s + 5):  # bounds a slow trickle of bytes
                async with client.stream(
                    cfg.method,
                    request.url,
                    headers=headers,
                    content=cfg.body.encode() if cfg.body is not None else None,
                    timeout=cfg.timeout_s,
                ) as resp:
                    status = resp.status_code
                    if status == 304:
                        return result(resp, not_modified=True)
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > cfg.max_bytes:
                        return result(
                            resp,
                            error=FetchError(
                                FetchErrorKind.TOO_LARGE, f"{declared} bytes > {cfg.max_bytes}"
                            ),
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > cfg.max_bytes:
                            return result(
                                resp,
                                error=FetchError(
                                    FetchErrorKind.TOO_LARGE, f"body exceeds {cfg.max_bytes} bytes"
                                ),
                            )
                        chunks.append(chunk)
                    if not 200 <= status < 300:
                        err = FetchError(
                            FetchErrorKind.HTTP,
                            f"HTTP {status}",
                            status=status,
                            retry_after_s=parse_retry_after(resp.headers.get("retry-after")),
                        )
                        return result(resp, error=err)
                    return result(resp, b"".join(chunks))
        except Exception as exc:
            return result(None, error=classify(exc))
