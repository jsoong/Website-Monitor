"""What a signed-in check needs to know, and how a "you are signed out" page is recognised.

``LoginPlan`` is built by the engine for each check of a bookmark that has a macro or a
semi-attended session. The browser fetcher follows it: open the page on the bookmark's profile,
look for the login signal, and (only then, and only if the engine permits it) run the macro.

The signal is evaluated two ways that must agree: live on a Playwright page (to decide whether
to run the macro) and on the serialised HTML a static fetch returns.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pagewatch.models import LoginSignal, MacroDef

SignalKind = str  # "selector" | "url" | "text"


def signal_in_html(html: bytes | str, final_url: str, signal: LoginSignal | None) -> str | None:
    """Which part of ``signal`` shows in this page, or ``None``. Never raises on odd markup."""
    if signal is None:
        return None
    if signal.url_contains and signal.url_contains.lower() in final_url.lower():
        return "url"
    if not (signal.selector or signal.text) or not html:
        return None
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return None
    if signal.selector:
        try:
            if tree.cssselect(signal.selector):
                return "selector"
        except Exception:
            return None
    if signal.text:
        for bad in tree.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        text = re.sub(r"\s+", " ", tree.text_content())
        if signal.text.lower() in text.lower():
            return "text"
    return None


async def signal_in_page(page: Any, signal: LoginSignal | None) -> str | None:
    """The same test, live on a Playwright page."""
    if signal is None:
        return None
    if signal.url_contains and signal.url_contains.lower() in str(page.url).lower():
        return "url"
    if signal.selector:
        try:
            if await page.query_selector(signal.selector) is not None:
                return "selector"
        except Exception:
            pass
    if signal.text:
        try:
            text = re.sub(r"\s+", " ", await page.inner_text("body"))
        except Exception:
            text = ""
        if signal.text.lower() in text.lower():
            return "text"
    return None


@dataclass(frozen=True, slots=True)
class MacroPermit:
    """The engine's answer to "may the macro log in now?"."""

    allowed: bool
    reason: str | None = None  # `stored_password_off`, `login_rate_limited`, `login_blocked`


@dataclass(slots=True)
class LoginPlan:
    profile_dir: Path | None  # a persistent browser profile (macros, semi-attended sessions)
    signal: LoginSignal | None
    macro: MacroDef | None = None
    # Asked just before the macro would run; records the attempt so a crash cannot retry at once.
    permit_macro: Callable[[], Awaitable[MacroPermit]] | None = None
    # Told how the run ended; a failure blocks further automated logins until the owner resets.
    macro_finished: Callable[[bool], Awaitable[None]] | None = None
    resolve_secret: Callable[[str], Awaitable[str | None]] | None = None
