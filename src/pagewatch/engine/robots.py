"""robots.txt for Follow-Links (spec: Politeness and network: "Follow-Links crawling obeys it").

Single URLs the owner chose are never checked against robots.txt; only links a crawl *discovered*
are. Behaviour follows RFC 9309: a 4xx answer (no file) means everything is allowed; a 5xx or an
unreachable host means nothing is, for now (the answer is cached briefly and asked again).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from pagewatch.engine.logs import get_logger

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.robots")

AGENT = "PageWatch"
TTL_S = 24 * 3600.0  # a robots.txt we read
RETRY_S = 15 * 60.0  # an answer we could not get
MAX_BYTES = 512 * 1024  # RFC 9309: parse at least 500 KiB


class RobotsCache:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        # origin -> (expires at monotonic seconds, parser | None meaning "disallow everything")
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
        now = self.e.clock.monotonic()
        entry = self._cache.get(origin)
        if entry is None or entry[0] <= now:
            entry = await self._load(origin, now)
            self._cache[origin] = entry
        parser = entry[1]
        return parser is not None and parser.can_fetch(AGENT, url)

    async def _load(self, origin: str, now: float) -> tuple[float, RobotFileParser | None]:
        e = self.e
        client = e.static_fetcher.client_for(e.settings.global_proxy, True)
        try:
            resp = await client.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": e.settings.default_user_agent},
                timeout=15.0,
            )
        except (httpx.HTTPError, OSError) as exc:
            log.warning("robots_unreachable", origin=origin, error=str(exc)[:120])
            return now + RETRY_S, None
        if 500 <= resp.status_code < 600:
            log.warning("robots_server_error", origin=origin, status=resp.status_code)
            return now + RETRY_S, None
        parser = RobotFileParser()
        if resp.status_code == 200:
            parser.parse(resp.content[:MAX_BYTES].decode("utf-8", errors="replace").splitlines())
        else:  # 4xx: there is no robots.txt
            parser.parse([])
        return now + TTL_S, parser
