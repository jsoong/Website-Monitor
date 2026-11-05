"""Check-Macro replay (spec: Logins and Check-Macros).

A macro is an ordered list of steps run on a Playwright page before the check: ``goto``,
``fill``, ``click``, ``select``, ``press``, ``wait_for`` and ``assert_text``. Secret values are
``{"secret": "<key>"}`` and are read from the secret store at the moment of the step.

Two rules shape the error handling:

* A failed step stops the macro, and the error names the step and its selector. It is built from
  the *step*, never from Playwright's own message: Playwright's call log can repeat a value that
  was typed, and that value may be a password.
* Nothing here logs a filled value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from pagewatch.models import (
    AssertTextStep,
    ClickStep,
    FillStep,
    GotoStep,
    MacroStep,
    PressStep,
    SecretRef,
    SelectStep,
    WaitForStep,
)

SecretResolver = Callable[[str], Awaitable[str | None]]
POLL_S = 0.2


class MacroError(Exception):
    """A step failed. ``message`` is safe to store and show: it never holds a typed value."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(message)
        self.index = index
        self.message = message


def describe(step: MacroStep) -> str:
    """``fill #user`` / ``goto https://...``: the step without any value that was typed."""
    target = getattr(step, "selector", None) or getattr(step, "url", None) or ""
    if isinstance(step, PressStep):
        target = step.selector or step.key
    if isinstance(step, WaitForStep):
        target = step.selector or f"{step.ms} ms"
    if isinstance(step, AssertTextStep):
        target = step.selector or "the page"
    return f"{step.op} {target}".strip()


async def _fail_on_timeout(index: int, step: MacroStep, work: Awaitable[Any]) -> Any:
    try:
        return await work
    except MacroError:
        raise
    except Exception as exc:
        name = type(exc).__name__
        what = "timed out" if name == "TimeoutError" else f"failed ({name})"
        raise MacroError(index, f"step {index + 1} ({describe(step)}) {what}") from None


async def run_macro(
    page: Any, steps: list[MacroStep], resolve_secret: SecretResolver | None = None
) -> None:
    """Run ``steps`` on ``page``; raises ``MacroError`` at the first one that fails."""
    for index, step in enumerate(steps):
        ms = step.timeout_s * 1000
        if isinstance(step, GotoStep):
            await _fail_on_timeout(index, step, page.goto(step.url, wait_until="load", timeout=ms))
        elif isinstance(step, FillStep):
            value = step.value
            if isinstance(value, SecretRef):
                secret = await resolve_secret(value.secret) if resolve_secret else None
                if secret is None:
                    raise MacroError(
                        index,
                        f"step {index + 1} ({describe(step)}): no stored secret named "
                        f"{value.secret!r}",
                    )
                text = secret
            else:
                text = value
            await _fail_on_timeout(index, step, page.locator(step.selector).fill(text, timeout=ms))
        elif isinstance(step, ClickStep):
            await _fail_on_timeout(index, step, page.locator(step.selector).click(timeout=ms))
            with suppress(Exception):  # a click may start a navigation; let it finish
                await page.wait_for_load_state("load", timeout=ms)
        elif isinstance(step, SelectStep):
            await _fail_on_timeout(
                index, step, page.locator(step.selector).select_option(step.value, timeout=ms)
            )
        elif isinstance(step, PressStep):
            if step.selector:
                await _fail_on_timeout(
                    index, step, page.locator(step.selector).press(step.key, timeout=ms)
                )
            else:
                await _fail_on_timeout(index, step, page.keyboard.press(step.key))
            with suppress(Exception):
                await page.wait_for_load_state("load", timeout=ms)
        elif isinstance(step, WaitForStep):
            if step.selector is not None:
                await _fail_on_timeout(
                    index, step, page.locator(step.selector).wait_for(timeout=ms)
                )
            else:
                await page.wait_for_timeout(step.ms)
        else:
            await _assert_text(page, step, index)


async def _assert_text(page: Any, step: AssertTextStep, index: int) -> None:
    """The text must appear within the step's timeout (pages fill in after a click)."""
    deadline = asyncio.get_running_loop().time() + step.timeout_s
    locator = page.locator(step.selector or "body")
    wanted = step.text.lower()
    while True:
        try:
            seen = (await locator.inner_text(timeout=1000)).lower()
        except Exception:
            seen = ""
        if wanted in seen:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise MacroError(
                index, f"step {index + 1} ({describe(step)}): the text {step.text!r} did not appear"
            )
        await asyncio.sleep(POLL_S)
