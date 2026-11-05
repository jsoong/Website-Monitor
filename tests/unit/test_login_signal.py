"""Recognising a login page: by selector, by redirect, by text, on HTML and on a live page."""

from __future__ import annotations

from typing import Any

import pytest

from pagewatch.engine.fetch.login import signal_in_html, signal_in_page
from pagewatch.models import LoginSignal

LOGIN_PAGE = (
    "<html><body><form><input name=username><input type=password id=pw></form>"
    "<p>Please sign in</p><script>var hidden = 'Session expired';</script></body></html>"
)
ACCOUNT = "<html><body><h1>Account</h1><p>Balance: 100</p></body></html>"


@pytest.mark.parametrize(
    "signal,expected",
    [
        (LoginSignal(selector="input[type=password]"), "selector"),
        (LoginSignal(text="please sign in"), "text"),
        (LoginSignal(url_contains="/login"), "url"),
        (LoginSignal(selector=".nope", text="nothing like it"), None),
    ],
)
def test_a_login_page_matches_its_signal(signal: LoginSignal, expected: str | None) -> None:
    assert signal_in_html(LOGIN_PAGE, "http://site.test/login?next=/a", signal) == expected


def test_the_account_page_matches_nothing() -> None:
    signal = LoginSignal(
        selector="input[type=password]", text="please sign in", url_contains="/login"
    )
    assert signal_in_html(ACCOUNT, "http://site.test/account", signal) is None


def test_script_text_does_not_count_as_page_text() -> None:
    assert signal_in_html(LOGIN_PAGE, "http://x/", LoginSignal(text="session expired")) is None


def test_no_signal_and_unreadable_pages_are_never_a_login() -> None:
    assert signal_in_html(LOGIN_PAGE, "http://x/login", None) is None
    assert signal_in_html(b"", "http://x/", LoginSignal(selector="a")) is None
    assert signal_in_html(b"\x00\xff not html <<<", "http://x/", LoginSignal(text="x")) is None


def test_a_signal_needs_something_to_look_for() -> None:
    with pytest.raises(ValueError, match="needs a selector"):
        LoginSignal()
    with pytest.raises(ValueError, match="needs a selector"):
        LoginSignal(selector="  ", text="")
    with pytest.raises(ValueError, match="invalid css selector"):
        LoginSignal(selector="div[")


class FakePage:
    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self._html = html

    async def query_selector(self, selector: str) -> Any:
        return (
            object()
            if selector == "input[type=password]" and "type=password" in self._html
            else None
        )

    async def inner_text(self, selector: str) -> str:
        return "Please sign in" if "Please sign in" in self._html else "Balance"


async def test_the_same_signal_on_a_live_page() -> None:
    login = FakePage("http://site.test/login", LOGIN_PAGE)
    account = FakePage("http://site.test/account", ACCOUNT)
    for signal, kind in [
        (LoginSignal(url_contains="/login"), "url"),
        (LoginSignal(selector="input[type=password]"), "selector"),
        (LoginSignal(text="please sign in"), "text"),
    ]:
        assert await signal_in_page(login, signal) == kind
        assert await signal_in_page(account, signal) is None
    assert await signal_in_page(login, None) is None
