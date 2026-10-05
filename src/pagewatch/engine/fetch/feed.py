"""RSS and Atom feeds: fetched like any page; optionally their enclosures are downloaded.

The feed XML itself goes through the pipeline (one block per entry, see ``pipeline/feeds.py``).
With ``feed.download_enclosures`` every enclosure not yet in ``feed.enclosures_dir`` is saved
there (capped per file and per check), so a podcast or a PDF attachment is kept when it appears.
A failed download is logged and never fails the check.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pagewatch.engine.fetch.base import FetchRequest, FetchResult
from pagewatch.engine.fetch.static import StaticFetcher
from pagewatch.engine.logs import get_logger
from pagewatch.engine.pipeline import feeds
from pagewatch.models import FeedOptions

log = get_logger("pagewatch.fetch.feed")

MAX_PER_CHECK = 25
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def enclosure_name(url: str) -> str:
    """A stable, filesystem-safe file name: a short hash of the URL plus its base name."""
    base = os.path.basename(unquote(urlsplit(url).path)) or "enclosure"
    return f"{hashlib.sha1(url.encode()).hexdigest()[:10]}-{_SAFE.sub('_', base)[:80]}"


class FeedFetcher:
    def __init__(self, static: StaticFetcher) -> None:
        self._static = static

    async def aclose(self) -> None:
        return None  # the static fetcher owns the clients and is closed by the engine

    async def fetch(self, request: FetchRequest) -> FetchResult:
        result = await self._static.fetch(request)
        opts = request.resolved.fetch.feed
        if result.error is None and not result.not_modified and opts.download_enclosures:
            try:
                await self._download(request, result.body, opts)
            except Exception:
                log.warning("enclosure_download_failed", url=request.url, exc_info=True)
        return result

    async def _download(self, request: FetchRequest, body: bytes, opts: FeedOptions) -> None:
        assert opts.enclosures_dir
        _parsed, entries = await asyncio.to_thread(feeds.feed_entries, body, opts)
        urls: list[str] = []
        for entry in entries:
            for enc in entry.get("enclosures") or []:
                href = str(enc.get("href") or "")
                if href.lower().startswith(("http://", "https://")) and href not in urls:
                    urls.append(href)
        folder = Path(opts.enclosures_dir)
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        cfg = request.resolved.fetch
        client = self._static.client_for(cfg.proxy or request.settings.global_proxy, cfg.verify_tls)
        fetched = 0
        for url in urls:
            target = folder / enclosure_name(url)
            if target.exists():
                continue
            if fetched >= MAX_PER_CHECK:
                break
            fetched += 1
            part = target.with_suffix(target.suffix + ".part")
            try:
                total = 0
                ua = {"User-Agent": request.settings.default_user_agent}
                async with client.stream("GET", url, timeout=cfg.timeout_s, headers=ua) as resp:
                    if resp.status_code != 200:
                        log.info("enclosure_skipped", url=url, status=resp.status_code)
                        continue
                    with part.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > opts.enclosure_max_bytes:
                                raise ValueError("enclosure exceeds the size cap")
                            fh.write(chunk)
                part.replace(target)
            except Exception as exc:
                log.info("enclosure_failed", url=url, error=str(exc)[:200])
                part.unlink(missing_ok=True)
