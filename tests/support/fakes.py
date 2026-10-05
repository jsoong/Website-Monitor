"""Test doubles: a scripted fetcher, and a fake Playwright-shaped browser for the manager."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.fetch.browser import LaunchPlan
from pagewatch.models import FetchErrorKind


def ok(
    request: FetchRequest, body: str | bytes, *, png: bytes | None = None,
    content_type: str = "text/html; charset=utf-8",
) -> FetchResult:  # fmt: skip
    data = body.encode() if isinstance(body, str) else body
    return FetchResult(request.url, 200, {}, content_type, data, screenshot_png=png, elapsed_ms=5)


def error(request: FetchRequest, kind: FetchErrorKind, message: str) -> FetchResult:
    return FetchResult(request.url, None, {}, "", b"", error=FetchError(kind, message))


class ScriptedFetcher:
    """A fetcher whose answer is computed by ``respond(request, call_number)``."""

    def __init__(self, respond: Callable[[FetchRequest, int], FetchResult]) -> None:
        self.respond = respond
        self.requests: list[FetchRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def fetch(self, request: FetchRequest) -> FetchResult:
        self.requests.append(request)
        return self.respond(request, len(self.requests))

    async def aclose(self) -> None:
        return None


# -- a fake browser ---------------------------------------------------------------------


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False
        self.headers: dict[str, str] | None = None
        self.routed = False

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.headers = headers

    async def route(self, pattern: str, handler: Any) -> None:
        self.routed = True

    async def close(self) -> None:
        self.closed = True
        self.context.browser.live_pages -= 1


class FakeContext:
    def __init__(self, browser: FakeBrowser, options: dict[str, Any]) -> None:
        self.browser = browser
        self.options = options
        self.closed = False
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = self.browser.page_factory(self) if self.browser.page_factory else FakePage(self)
        self.pages.append(page)
        self.browser.live_pages += 1
        self.browser.max_live = max(self.browser.max_live, self.browser.live_pages)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(
        self, plan: LaunchPlan, page_factory: Callable[[FakeContext], FakePage] | None = None
    ) -> None:
        self.plan = plan
        self.page_factory = page_factory
        self.contexts: list[FakeContext] = []
        self.closed = False
        self.connected = True
        self.live_pages = 0
        self.max_live = 0

    def is_connected(self) -> bool:
        return self.connected and not self.closed

    async def new_context(self, **options: Any) -> FakeContext:
        ctx = FakeContext(self, options)
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class FakeLauncher:
    """Launches ``FakeBrowser``s; ``fail`` names plan sources that raise instead."""

    def __init__(
        self,
        fail: dict[str, str] | None = None,
        page_factory: Callable[[FakeContext], FakePage] | None = None,
    ) -> None:
        self.fail = dict(fail or {})
        self.page_factory = page_factory
        self.plans: list[LaunchPlan] = []
        self.browsers: list[FakeBrowser] = []
        self.closed = False

    async def __call__(self, plan: LaunchPlan) -> FakeBrowser:
        self.plans.append(plan)
        if plan.source in self.fail:
            raise RuntimeError(self.fail[plan.source])
        browser = FakeBrowser(plan, self.page_factory)
        self.browsers.append(browser)
        return browser

    async def aclose(self) -> None:
        self.closed = True
