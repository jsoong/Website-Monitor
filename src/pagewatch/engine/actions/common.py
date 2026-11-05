"""Small helpers every action uses."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.clock import parse_iso
from pagewatch.models import PWModel

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

TRANSIENT_STATUS = {408, 425, 429}


def parse_params[P: PWModel](model: type[P], params: dict[str, Any], what: str) -> P:
    """Validate an action's parameters; a bad stored configuration is a final failure that the
    Problems list explains, not a retry loop."""
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        msg = str(first["msg"]).removeprefix("Value error, ")
        raise ActionConfigError(f"{what}: {where + ': ' if where else ''}{msg}") from exc


async def get_secret(engine: Engine, key: str, what: str) -> str:
    """A secret by key name, read off the event loop (the credential store can block)."""
    value = await asyncio.to_thread(engine.secrets.get, key)
    if value is None:
        raise ActionConfigError(
            f"{what}: no stored secret named {key!r} (enter it under Settings, Notifications)"
        )
    return value


def detected_at(ctx: AlertContext) -> datetime:
    return parse_iso(ctx.detected_at)


def local_time(engine: Engine, when: datetime) -> datetime:
    """Naive local wall time in the engine's zone (the user's, or the system's)."""
    return engine.zone.to_local(when)


def template_values(ctx: AlertContext) -> dict[str, str]:
    """The ``{var}`` values shared by export paths and program arguments."""
    return {
        "id": str(ctx.bookmark_id),
        "name": ctx.name,
        "url": ctx.url,
        "change_id": str(ctx.change_id),
        "info1": ctx.info[0] or "",
        "info2": ctx.info[1] or "",
        "info3": ctx.info[2] or "",
    }


def truncate_utf8(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def host_of(url: str) -> str:
    return urlsplit(url).netloc or url[:60]


async def http_send(
    method: str,
    url: str,
    *,
    what: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 15.0,
    verify_tls: bool = True,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> httpx.Response:
    """One request, no redirects (a redirect would drop the body or forward credentials) and no
    ambient proxy settings. 2xx is success; timeouts, 408/425/429 and 5xx are transient (the
    queue retries); any other status is a configuration problem and fails the job at once.
    Messages carry the host only: webhook URLs often hold a secret in the path."""
    host = host_of(url)
    try:
        async with client_factory(
            timeout=timeout_s, verify=verify_tls, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.request(method, url, headers=headers, content=body)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"{what}: {host} did not answer in {timeout_s:g} s") from exc
    except httpx.HTTPError as exc:
        raise ConnectionError(f"{what}: could not reach {host} ({type(exc).__name__})") from exc
    status = response.status_code
    if 200 <= status < 300:
        return response
    snippet = " ".join(response.text.split())[:200]
    message = f"{what}: {host} answered HTTP {status}" + (f": {snippet}" if snippet else "")
    if status in TRANSIENT_STATUS or status >= 500:
        raise ConnectionError(message)
    if 300 <= status < 400:
        message += " (a redirect: use the final URL)"
    raise ActionConfigError(message)


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` so that a reader sees the old file or the whole new one, never half of it:
    temp file in the same folder, flushed to disk, then renamed over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
