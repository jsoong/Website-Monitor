"""Record a Check-Macro with Playwright codegen (spec: Logins and Check-Macros -> Recording).

"Record macro" runs ``playwright codegen --target jsonl`` on the owner's desktop. When they close
the codegen window, its output (one JSON object per recorded action) is converted into macro
steps for review. The converter is a pure function so it can be tested without a browser; the
action shapes (``navigate``, ``click``, ``fill``, ``press``, ``select``, ``check``,
``assertText``...) are those of Playwright's own recorder.

**A recorded password is never kept.** A ``fill`` into a password-looking field becomes
``{"secret": "<key>"}``; the typed text is dropped on the spot, and a warning tells the owner to
store the password in the credential store under that key.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from pagewatch.engine.frozen import playwright_command
from pagewatch.engine.logs import get_logger
from pagewatch.models import MacroStep, RecordOut

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.recorder")

_PASSWORDISH = re.compile(r"password|passwd|passcode|pwd|secret", re.IGNORECASE)
_STEP: TypeAdapter[Any] = TypeAdapter(MacroStep)
RECORD_TIMEOUT_S = 30 * 60.0
MAX_RECORDINGS = 8


@dataclass(slots=True)
class Converted:
    steps: list[MacroStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def convert_codegen(text: str, *, password_secret_key: str = "macro-password") -> Converted:
    """Playwright codegen JSONL -> macro steps, with a warning for everything left out."""
    out = Converted()
    skipped: dict[str, int] = {}
    password_seen = False

    def add(step: dict[str, Any]) -> None:
        try:
            out.steps.append(_STEP.validate_python(step))
        except ValidationError as exc:
            out.warnings.append(f"a {step.get('op')} step was left out: {exc.errors()[0]['msg']}")

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            action = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(action, dict) or "name" not in action:
            continue  # the header line holds launch options, not an action
        name = str(action["name"])
        selector = action.get("selector")
        if name in ("navigate", "openPage"):
            url = str(action.get("url") or "")
            if url.lower().startswith(("http://", "https://")) and not (
                out.steps and getattr(out.steps[-1], "url", None) == url
            ):
                add({"op": "goto", "url": url})
        elif name in ("click", "check", "uncheck") and selector:
            if action.get("button", "left") != "left":
                skipped["a right or middle click"] = skipped.get("a right or middle click", 0) + 1
                continue
            add({"op": "click", "selector": selector})
        elif name == "fill" and selector:
            if _PASSWORDISH.search(str(selector)):
                password_seen = True
                add({"op": "fill", "selector": selector, "value": {"secret": password_secret_key}})
            else:
                add({"op": "fill", "selector": selector, "value": str(action.get("text", ""))})
        elif name == "press" and action.get("key"):
            if action.get("modifiers"):
                out.warnings.append(f"the modifier keys on a {action['key']} press were dropped")
            step: dict[str, Any] = {"op": "press", "key": str(action["key"])}
            if selector:
                step["selector"] = selector
            add(step)
        elif name == "select" and selector and action.get("options"):
            add({"op": "select", "selector": selector, "value": str(action["options"][0])})
        elif name == "assertText" and action.get("text"):
            step = {"op": "assert_text", "text": str(action["text"])}
            if selector:
                step["selector"] = selector
            add(step)
        elif name == "assertVisible" and selector:
            add({"op": "wait_for", "selector": selector})
        elif name != "closePage":
            skipped[name] = skipped.get(name, 0) + 1
    for what, n in sorted(skipped.items()):
        out.warnings.append(
            f"{n} recorded action(s) of kind {what!r} are not supported and were left out"
        )
    if password_seen:
        out.warnings.append(
            f"A password field was recorded. The typed password was NOT kept: store it in the "
            f"credential store under the name {password_secret_key!r}."
        )
    return out


CommandFactory = Callable[[str, Path], list[str]]


def codegen_command(channel: str | None) -> CommandFactory:
    """``playwright codegen`` through Playwright's bundled Node driver, not ``python -m
    playwright``: in the packaged build there is no Python to run ``-m`` with (the running program
    is the engine itself). See ``engine/frozen.py``."""

    def build(url: str, out: Path) -> list[str]:
        args = ["codegen", "--target", "jsonl", "-o", str(out)]
        if channel:
            args += ["--channel", channel]
        return playwright_command(*args, url).argv

    return build


@dataclass(slots=True)
class _Recording:
    id: str
    state: str = "running"
    steps: list[MacroStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    out: Path | None = None

    def view(self) -> RecordOut:
        return RecordOut(
            id=self.id,
            state=self.state,
            steps=self.steps,
            warnings=self.warnings,
            error=self.error,
        )


class MacroRecorder:
    def __init__(self, engine: Engine, command: CommandFactory | None = None) -> None:
        self.e = engine
        self._command = command
        self._recordings: dict[str, _Recording] = {}

    def set_command(self, command: CommandFactory) -> None:
        self._command = command

    async def start(self, url: str, password_secret_key: str) -> RecordOut:
        live = [r for r in self._recordings.values() if r.state == "running"]
        if live:
            raise RuntimeError("a recording is already running; finish or cancel it first")
        while len(self._recordings) >= MAX_RECORDINGS:
            self._recordings.pop(next(iter(self._recordings)))
        rec = _Recording(uuid.uuid4().hex[:12])
        self._recordings[rec.id] = rec
        out = Path(tempfile.mkdtemp(prefix="pagewatch-rec-")) / "codegen.jsonl"
        rec.out = out
        build = self._command or codegen_command(self.e.settings.browser_channel)
        try:
            rec.proc = await asyncio.create_subprocess_exec(
                *build(url, out),
                env=playwright_command().env if self._command is None else None,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            rec.state, rec.error = "failed", f"could not start Playwright codegen: {exc.strerror}"
            return rec.view()
        rec.task = asyncio.create_task(self._wait(rec, password_secret_key), name=f"rec-{rec.id}")
        return rec.view()

    async def _wait(self, rec: _Recording, key: str) -> None:
        assert rec.proc is not None and rec.out is not None
        try:
            async with asyncio.timeout(RECORD_TIMEOUT_S):
                await rec.proc.wait()
        except TimeoutError:
            rec.proc.kill()
            rec.state, rec.error = "failed", "the recording was left open for too long"
            return
        except asyncio.CancelledError:
            raise
        if rec.state == "cancelled":
            return
        try:
            text = await asyncio.to_thread(lambda: (rec.out or Path()).read_text(encoding="utf-8"))
        except OSError:
            text = ""
        converted = convert_codegen(text, password_secret_key=key)
        if not converted.steps:
            rec.state = "failed"
            rec.error = "nothing was recorded (the window was closed before any step)"
            rec.warnings = converted.warnings
            return
        rec.steps, rec.warnings, rec.state = converted.steps, converted.warnings, "done"
        log.info("macro_recorded", steps=len(rec.steps))

    def get(self, rec_id: str) -> RecordOut | None:
        rec = self._recordings.get(rec_id)
        return rec.view() if rec else None

    async def cancel(self, rec_id: str) -> RecordOut | None:
        rec = self._recordings.get(rec_id)
        if rec is None:
            return None
        if rec.state == "running":
            rec.state = "cancelled"
            if rec.proc is not None and rec.proc.returncode is None:
                with suppress(ProcessLookupError):
                    rec.proc.kill()
            if rec.task is not None:
                rec.task.cancel()
                await asyncio.gather(rec.task, return_exceptions=True)
        return rec.view()

    async def aclose(self) -> None:
        for rec_id in list(self._recordings):
            await self.cancel(rec_id)
