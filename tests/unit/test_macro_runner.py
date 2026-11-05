"""The macro interpreter, on a scripted page: step order, secrets, and error messages."""

from __future__ import annotations

from typing import Any

import pytest

from pagewatch.engine.fetch.macro import MacroError, describe, run_macro
from pagewatch.models import MacroDef

SECRET = "hunter2-the-password"


class PWTimeout(Exception):
    """Named like Playwright's TimeoutError so the interpreter treats it as a timeout. It
    also carries the typed value in its text, as Playwright's call log can."""


PWTimeout.__name__ = "TimeoutError"


class Locator:
    def __init__(self, page: Page, selector: str) -> None:
        self.page, self.selector = page, selector

    def _check(self) -> None:
        if self.selector in self.page.missing:
            raise PWTimeout(f"Locator.fill: Timeout exceeded. - fill({SECRET!r})")

    async def fill(self, value: str, timeout: float) -> None:
        self._check()
        self.page.log.append(("fill", self.selector, value))

    async def click(self, timeout: float) -> None:
        self._check()
        self.page.log.append(("click", self.selector, None))

    async def select_option(self, value: str, timeout: float) -> None:
        self._check()
        self.page.log.append(("select", self.selector, value))

    async def press(self, key: str, timeout: float) -> None:
        self._check()
        self.page.log.append(("press", self.selector, key))

    async def wait_for(self, timeout: float) -> None:
        self._check()
        self.page.log.append(("wait_for", self.selector, None))

    async def inner_text(self, timeout: float) -> str:
        self.page.reads += 1
        return self.page.text_after.get(self.page.reads, self.page.text)


class Keyboard:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def press(self, key: str) -> None:
        self.page.log.append(("keyboard", None, key))


class Page:
    def __init__(self, missing: set[str] | None = None, text: str = "Welcome") -> None:
        self.log: list[tuple[str, str | None, str | None]] = []
        self.missing = missing or set()
        self.text = text
        self.text_after: dict[int, str] = {}
        self.reads = 0
        self.keyboard = Keyboard(self)

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    async def goto(self, url: str, wait_until: str, timeout: float) -> None:
        self.log.append(("goto", url, None))

    async def wait_for_load_state(self, state: str, timeout: float) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        self.log.append(("sleep", None, str(ms)))


def macro(*steps: dict[str, Any]) -> MacroDef:
    return MacroDef.model_validate({"name": "m", "steps": list(steps)})


LOGIN = [
    {"op": "goto", "url": "http://site.test/login"},
    {"op": "fill", "selector": "#user", "value": "alice"},
    {"op": "fill", "selector": "#pw", "value": {"secret": "portal-pw"}},
    {"op": "click", "selector": "#go"},
    {"op": "select", "selector": "#lang", "value": "en"},
    {"op": "press", "key": "Enter"},
    {"op": "press", "selector": "#pw", "key": "Tab"},
    {"op": "wait_for", "selector": ".account"},
    {"op": "wait_for", "ms": 250},
    {"op": "assert_text", "text": "welcome"},
]


async def test_every_step_kind_runs_in_order_and_the_secret_is_resolved_at_the_step() -> None:
    page = Page()
    asked: list[str] = []

    async def secrets(key: str) -> str | None:
        asked.append(key)
        return SECRET

    await run_macro(page, macro(*LOGIN).steps, secrets)
    assert [(op, arg) for op, arg, _ in page.log] == [
        ("goto", "http://site.test/login"), ("fill", "#user"), ("fill", "#pw"),
        ("click", "#go"), ("select", "#lang"), ("keyboard", None), ("press", "#pw"),
        ("wait_for", ".account"), ("sleep", None),
    ]  # fmt: skip
    assert asked == ["portal-pw"] and ("fill", "#pw", SECRET) in page.log


async def test_a_failed_step_names_the_step_but_never_repeats_a_typed_value() -> None:
    page = Page(missing={"#pw"})

    async def secrets(key: str) -> str | None:
        return SECRET

    with pytest.raises(MacroError) as caught:
        await run_macro(page, macro(*LOGIN).steps, secrets)
    assert caught.value.index == 2
    assert caught.value.message == "step 3 (fill #pw) timed out"
    assert SECRET not in caught.value.message and SECRET not in repr(caught.value)
    assert caught.value.__cause__ is None  # the original text (with the value) is dropped
    assert not any(op == "click" for op, _, _ in page.log)  # nothing ran after the failure


async def test_a_missing_secret_stops_the_macro_before_typing_anything() -> None:
    page = Page()

    async def secrets(key: str) -> str | None:
        return None

    with pytest.raises(MacroError, match="no stored secret named 'portal-pw'"):
        await run_macro(page, macro(*LOGIN).steps, secrets)
    assert ("fill", "#pw", None) not in page.log
    assert [op for op, _, _ in page.log] == ["goto", "fill"]  # only the user name went in


async def test_no_resolver_means_a_secret_step_fails_cleanly() -> None:
    with pytest.raises(MacroError, match="no stored secret"):
        await run_macro(Page(), macro(LOGIN[2]).steps, None)


async def test_assert_text_waits_for_the_text_to_appear_and_then_gives_up() -> None:
    page = Page(text="Loading...")
    page.text_after = {3: "Hello, Welcome back"}
    await run_macro(page, macro({"op": "assert_text", "text": "welcome back"}).steps)
    assert page.reads == 3
    gone = Page(text="Nothing here")
    with pytest.raises(MacroError, match="did not appear"):
        await run_macro(
            gone, macro({"op": "assert_text", "text": "welcome", "timeout_s": 0.5}).steps
        )


def test_describe_never_includes_a_value() -> None:
    steps = macro(*LOGIN).steps
    assert [describe(s) for s in steps] == [
        "goto http://site.test/login", "fill #user", "fill #pw", "click #go", "select #lang",
        "press Enter", "press #pw", "wait_for .account", "wait_for 250 ms", "assert_text the page",
    ]  # fmt: skip
    assert all("alice" not in describe(s) and SECRET not in describe(s) for s in steps)


def test_a_password_field_cannot_be_filled_with_an_inline_value() -> None:
    with pytest.raises(ValueError, match="password field"):
        macro({"op": "fill", "selector": "input[name=password]", "value": "plain"})
    macro({"op": "fill", "selector": "input[name=password]", "value": {"secret": "k"}})


def test_macro_shape_rules() -> None:
    with pytest.raises(ValueError):
        macro({"op": "wait_for"})  # neither a selector nor a time
    with pytest.raises(ValueError):
        macro({"op": "wait_for", "selector": "a", "ms": 5})  # both
    with pytest.raises(ValueError):
        macro({"op": "goto", "url": "ftp://x/"})
    with pytest.raises(ValueError):
        macro({"op": "teleport"})
    with pytest.raises(ValueError):
        MacroDef.model_validate({"name": "m", "steps": []})
    with pytest.raises(ValueError):
        macro({"op": "fill", "selector": "#a", "value": {"secret": "bad key!"}})
    m = macro(*LOGIN)
    assert m.secret_keys == ["portal-pw"] and m.uses_stored_password
