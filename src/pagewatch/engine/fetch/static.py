"""Static HTTP(S) fetcher: httpx, HTTP/2, conditional GET, hard body cap."""

from __future__ import annotations

import asyncio
import email.utils
import ipaddress
import socket
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pagewatch.engine.fetch import cookies as cookie_files
from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.fetch.proxy import ProxyError, resolve_proxy
from pagewatch.engine.secrets import NullSecrets, SecretStore
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


def is_local_host(host: str | None) -> bool:
    """Loopback or a private-network address: a password sent there does not cross the internet."""
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _new_client(proxy: str | None, verify: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        verify=verify,
        proxy=proxy,
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
        timeout=httpx.Timeout(30.0),
    )


class StaticFetcher:
    def __init__(self, secrets: SecretStore | None = None) -> None:
        self._secrets: SecretStore = secrets or NullSecrets()
        self._clients: dict[tuple[str | None, bool], httpx.AsyncClient] = {}
        # A bookmark with cookies or a server login keeps a client (and cookie jar) of its own,
        # so its session never mixes with another bookmark's on the same website.
        self._sessions: dict[int, tuple[tuple[object, ...], httpx.AsyncClient]] = {}

    def use_secrets(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def client_for(self, proxy: str | None, verify: bool) -> httpx.AsyncClient:
        """The pooled client for these transport settings (also used for enclosure downloads)."""
        key = (proxy, verify)
        client = self._clients.get(key)
        if client is None:
            client = _new_client(proxy, verify)
            self._clients[key] = client
        return client

    async def _session_client(
        self, bookmark_id: int, proxy: str | None, verify: bool, cookie_file: Path | None
    ) -> httpx.AsyncClient:
        """The bookmark's own client. It is rebuilt when its proxy, TLS setting or cookie file
        changes (re-importing cookies must take effect on the next check)."""
        mtime = None
        if cookie_file is not None:
            try:
                mtime = (await asyncio.to_thread(cookie_file.stat)).st_mtime_ns
            except OSError as exc:
                raise cookie_files.CookieFileError(
                    f"the cookie file is missing ({exc.strerror or exc}); import it again"
                ) from exc
        signature: tuple[object, ...] = (proxy, verify, str(cookie_file), mtime)
        held = self._sessions.get(bookmark_id)
        if held is not None and held[0] == signature:
            return held[1]
        client = _new_client(proxy, verify)
        if cookie_file is not None:
            parsed = await asyncio.to_thread(cookie_files.load_cookie_file, cookie_file)
            cookie_files.add_to_httpx(client.cookies, parsed.cookies, time.time())
        self._sessions[bookmark_id] = (signature, client)
        if held is not None:
            await held[1].aclose()
        return client

    def forget_session(self, bookmark_id: int) -> None:
        """Drop a bookmark's cookie jar (wipe session, bookmark deleted). The client is closed
        by the next ``aclose``; an idle httpx client holds no sockets worth chasing."""
        self._sessions.pop(bookmark_id, None)

    async def aclose(self) -> None:
        clients = list(self._clients.values()) + [c for _, c in self._sessions.values()]
        self._clients, self._sessions = {}, {}
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

        def refuse(message: str) -> FetchResult:
            # a configuration problem, not the website's: PARSE is the engine's "config" kind
            elapsed = int((time.perf_counter() - started) * 1000)
            return FetchResult(
                request.url, None, {}, "", b"", elapsed_ms=elapsed,
                error=FetchError(FetchErrorKind.PARSE, message),
            )  # fmt: skip

        try:
            proxy = await resolve_proxy(cfg, request.settings, self._secrets)
        except ProxyError as exc:
            return refuse(str(exc))
        proxy_url = proxy.url() if proxy else None
        auth: httpx.Auth | None = None
        if cfg.auth is not None:
            password = await asyncio.to_thread(self._secrets.get, cfg.auth.secret_key)
            if password is None:
                return refuse(f"no stored secret named {cfg.auth.secret_key!r} for the login")
            if cfg.auth.kind == "basic":
                if urlsplit(request.url).scheme == "http" and not is_local_host(
                    urlsplit(request.url).hostname
                ):
                    return refuse(
                        "refusing to send a Basic password over plain http; use https:// or Digest"
                    )
                auth = httpx.BasicAuth(cfg.auth.username, password)
            else:
                auth = httpx.DigestAuth(cfg.auth.username, password)
        try:
            if (cfg.cookies_file or cfg.auth) and request.bookmark_id is not None:
                client = await self._session_client(
                    request.bookmark_id,
                    proxy_url,
                    cfg.verify_tls,
                    Path(cfg.cookies_file) if cfg.cookies_file else None,
                )
            elif cfg.cookies_file:  # a preview: cookies for this one request, then forgotten
                client = _new_client(proxy_url, cfg.verify_tls)
                parsed = await asyncio.to_thread(cookie_files.load_cookie_file, cfg.cookies_file)
                cookie_files.add_to_httpx(client.cookies, parsed.cookies, time.time())
            else:
                client = self.client_for(proxy_url, cfg.verify_tls)
        except cookie_files.CookieFileError as exc:
            return refuse(str(exc))

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
                    auth=auth,
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
