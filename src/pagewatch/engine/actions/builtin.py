"""The action implementations available in this build, keyed by ``ActionType`` value."""

from __future__ import annotations

import sys
import webbrowser
from typing import TYPE_CHECKING

from pagewatch.engine.actions.base import Action, ActionUnavailable, AlertContext

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine


async def toast(engine: Engine, ctx: AlertContext) -> None:
    await engine.toasts.notify(ctx.bookmark_id, ctx.title, ctx.body, ctx.change_id)


async def sound(engine: Engine, ctx: AlertContext) -> None:
    path = str(ctx.params.get("path", ""))
    if sys.platform == "win32" and path:  # pragma: no cover
        import winsound

        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)


async def open_(engine: Engine, ctx: AlertContext) -> None:
    if ctx.params.get("target", "internal") == "external":
        webbrowser.open(ctx.url)
    else:
        engine.events.publish(
            "open_change", {"bookmark_id": ctx.bookmark_id, "change_id": ctx.change_id}
        )


async def mark_read(engine: Engine, ctx: AlertContext) -> None:
    await engine.mark_read(ctx.bookmark_id)


def _unavailable(name: str, milestone: str) -> Action:
    async def run(engine: Engine, ctx: AlertContext) -> None:
        raise ActionUnavailable(f"the {name} action is not available in this build ({milestone})")

    return run


ACTIONS: dict[str, Action] = {
    "toast": toast,
    "sound": sound,
    "open": open_,
    "mark_read": mark_read,
    "email": _unavailable("email", "planned for M6"),
    "export": _unavailable("export", "planned for M6"),
    "run_program": _unavailable("run_program", "planned for M6"),
    "ntfy": _unavailable("ntfy", "planned for M6"),
    "scrape": _unavailable("scrape", "planned for M6"),
    "script": _unavailable("script", "planned for M6"),
    "webhook": _unavailable("webhook", "after v1"),
    "pushover": _unavailable("pushover", "after v1"),
}
