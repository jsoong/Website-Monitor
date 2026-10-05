"""Which fetcher serves a bookmark, and what ``check_run.method`` calls the check.

The transport comes from the URL scheme (``file:``, ``ftp:``/``ftps:``, ``http:``/``https:``);
for web pages the bookmark's ``check_method`` picks static, browser or screenshot. What the bytes
*are* (a PDF, a feed, records) is decided later from the content, so the method recorded for a
web check can be refined once the content is known (``method_kind``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pagewatch.engine.fetch.base import Fetcher
from pagewatch.models import CheckKind, FetchConfig

RouteName = Literal["static", "feed", "browser", "screenshot", "ftp", "file"]


@dataclass(frozen=True, slots=True)
class Route:
    name: RouteName
    kind: CheckKind  # what check_run.method says before the content is known


STATIC = Route("static", CheckKind.STATIC)
BROWSER = Route("browser", CheckKind.BROWSER)


def route_for(
    url: str, source_type: str, check_method: str, cfg: FetchConfig | None = None
) -> Route | None:
    """``None`` for a scheme nothing can fetch."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return None
    if scheme == "file":
        return Route("file", CheckKind.FILE)
    if scheme in ("ftp", "ftps"):
        return Route("ftp", CheckKind.FTP)
    if scheme not in ("http", "https"):
        return None
    if check_method == "screenshot":
        return Route("screenshot", CheckKind.SCREENSHOT)
    if check_method == "browser":
        return BROWSER
    if source_type == "feed" and cfg is not None and cfg.feed.download_enclosures:
        return Route("feed", CheckKind.FEED)
    return STATIC


def method_kind(route: Route, source_kind: str | None) -> CheckKind:
    """``check_run.method`` once the content is known: documents, feeds and records are named
    after what they are (spec: the method column), but a file or FTP check stays a file or FTP
    check, and a browser or screenshot check stays one."""
    if route.name in ("file", "ftp", "browser", "screenshot"):
        return route.kind
    if source_kind in ("pdf", "docx", "xlsx"):
        return CheckKind.DOCUMENT
    if source_kind == "feed":
        return CheckKind.FEED
    if source_kind == "records":
        return CheckKind.RECORDS
    return route.kind


class FetcherSet:
    """The engine's fetchers by route name. Tests replace entries with fakes."""

    def __init__(self, fetchers: dict[str, Fetcher]) -> None:
        self._fetchers = dict(fetchers)

    def get(self, route: Route) -> Fetcher:
        return self._fetchers[route.name]

    def replace(self, name: RouteName, fetcher: Fetcher) -> None:
        self._fetchers[name] = fetcher

    async def aclose(self) -> None:
        for fetcher in self._fetchers.values():
            await fetcher.aclose()
