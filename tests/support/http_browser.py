"""A browser-shaped stand-in for ``BrowserManager``: pages are HTTP clients with a DOM.

It lets the *real* ``BrowserFetcher``, macro interpreter and login plan run in the default test
suite against the real fixture login site (cookies, redirects, form posts, session expiry) without
launching Chromium. It understands exactly what those need: ``goto``, CSS locators, ``fill``,
``click`` on a submit button or a link, ``press`` Enter in a form field, waiting for a selector,
and per-profile cookie jars that persist between pages (a "persistent profile").

What it cannot do (run JavaScript, paint) is covered by the real-Chromium tests in
``tests/browser``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from lxml import html as lxml_html

from pagewatch.engine.fetch.browser import SessionSpec


class HttpTimeout(Exception):
    """Named like Playwright's TimeoutError so the macro interpreter treats it as one."""


HttpTimeout.__name__ = "TimeoutError"


class _Response:
    def __init__(self, resp: httpx.Response) -> None:
        self.status = resp.status_code
        self._headers = {k.lower(): v for k, v in resp.headers.items()}

    async def all_headers(self) -> dict[str, str]:
        return self._headers


class _Keyboard:
    def __init__(self, page: HttpPage) -> None:
        self._page = page

    async def press(self, key: str) -> None:
        if key == "Enter" and self._page.focused is not None:
            await self._page.submit_form_of(self._page.focused)


class _Locator:
    def __init__(self, page: HttpPage, selector: str) -> None:
        self.page, self.selector = page, selector

    def _el(self) -> Any:
        found = self.page.select(self.selector)
        if not found:
            raise HttpTimeout(f"nothing matches {self.selector}")
        return found[0]

    async def fill(self, value: str, timeout: float) -> None:
        el = self._el()
        self.page.values[el] = value
        self.page.focused = el

    async def click(self, timeout: float) -> None:
        el = self._el()
        self.page.focused = el
        if el.tag == "a" and el.get("href"):
            await self.page.goto(urljoin(self.page.url, el.get("href")), "load", timeout)
        elif el.tag in ("button", "input") and el.get("type", "submit") in ("submit", ""):
            await self.page.submit_form_of(el)

    async def select_option(self, value: str, timeout: float) -> None:
        self.page.values[self._el()] = value

    async def press(self, key: str, timeout: float) -> None:
        el = self._el()
        self.page.focused = el
        if key == "Enter":
            await self.page.submit_form_of(el)

    async def wait_for(self, timeout: float) -> None:
        self._el()

    async def inner_text(self, timeout: float) -> str:
        found = self.page.select(self.selector)
        return str(found[0].text_content()) if found else ""


class HttpPage:
    main_frame = object()

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.url = "about:blank"
        self.doc: Any = lxml_html.fromstring("<html><body></body></html>")
        self.values: dict[Any, str] = {}
        self.focused: Any = None
        self.keyboard = _Keyboard(self)
        self.frames = [self.main_frame]
        self.requests: list[tuple[str, str]] = []

    def select(self, selector: str) -> list[Any]:
        try:
            return list(self.doc.cssselect(selector))
        except Exception:
            return []

    def _adopt(self, resp: httpx.Response) -> _Response:
        self.url = str(resp.url)
        try:
            self.doc = lxml_html.fromstring(resp.content or b"<html></html>")
        except Exception:
            self.doc = lxml_html.fromstring("<html><body></body></html>")
        self.values, self.focused = {}, None
        return _Response(resp)

    async def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> _Response:
        self.requests.append(("GET", url))
        return self._adopt(await self.client.get(url))

    async def submit_form_of(self, el: Any) -> None:
        form = el if el.tag == "form" else el.getparent()
        while form is not None and form.tag != "form":
            form = form.getparent()
        if form is None:
            return
        data: dict[str, str] = {}
        for field in form.iter("input", "select", "textarea"):
            name = field.get("name")
            if name and field.get("type") != "submit":
                data[name] = self.values.get(field, field.get("value", ""))
        action = urljoin(self.url, form.get("action") or self.url)
        method = (form.get("method") or "get").upper()
        self.requests.append((method, action))
        if method == "POST":
            self._adopt(await self.client.post(action, data=data))
        else:
            self._adopt(await self.client.get(action, params=data))

    # -- the Playwright surface the fetcher and the macro interpreter use -----------------

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def query_selector(self, selector: str) -> Any:
        found = self.select(selector)
        return found[0] if found else None

    async def inner_text(self, selector: str) -> str:
        found = self.select(selector)
        return str(found[0].text_content()) if found else ""

    async def content(self) -> str:
        return str(lxml_html.tostring(self.doc, encoding="unicode"))

    async def wait_for_load_state(self, state: str = "load", timeout: float = 0) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.client.headers.update(headers)

    async def route(self, pattern: str, handler: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class HttpBrowser:
    """Quacks like ``BrowserManager`` for ``BrowserFetcher``."""

    def __init__(self) -> None:
        self.profiles: dict[Path, httpx.Cookies] = {}
        self.pages: list[HttpPage] = []
        self.state = "running"

    async def ensure(self) -> None:
        return None

    def note_success(self) -> None:
        return None

    def note_crash(self) -> None:
        return None

    def profile_cookies(self, profile_dir: Path) -> httpx.Cookies:
        return self.profiles.setdefault(profile_dir, httpx.Cookies())

    @asynccontextmanager
    async def page(
        self,
        *,
        verify_tls: bool = True,
        headers: dict[str, str] | None = None,
        block_media: bool = True,
        session: SessionSpec | None = None,
    ) -> AsyncIterator[HttpPage]:
        jar = httpx.Cookies()
        if session is not None and session.profile_dir is not None:
            if not session.profile_dir.exists():  # wiped (or never used): the profile starts empty
                self.profiles.pop(session.profile_dir, None)
            session.profile_dir.mkdir(parents=True, exist_ok=True)
            jar = httpx.Cookies(self.profile_cookies(session.profile_dir))
        client = httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=10)
        page = HttpPage(client)
        self.pages.append(page)
        try:
            yield page
        finally:
            if session is not None and session.profile_dir is not None:
                self.profiles[session.profile_dir] = client.cookies  # the profile keeps them
            await client.aclose()
