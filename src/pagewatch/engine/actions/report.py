"""The ``report`` action: write a report file when a change is detected.

Parameters: ``path`` (required; ``{id}`` ``{name}`` ``{date}`` ``{datetime}`` ``{template}``
are filled in), ``template`` (default ``changed``) and ``format`` (default: the template's own).
The file is replaced atomically. The action never moves the "last report" marker, so a burst of
changes rewrites one living file instead of leaving an empty report behind the first.
"""

from __future__ import annotations

import asyncio
import os
import re
import string
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pagewatch.engine import reports
from pagewatch.engine.actions.base import ActionUnavailable, AlertContext

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def expand(template: str, values: dict[str, str]) -> str:
    """``{name}`` placeholders only: no attribute or index access, unknown names are an error."""
    out: list[str] = []
    for literal, field, spec, conv in string.Formatter().parse(template):
        out.append(literal)
        if field is None:
            continue
        if spec or conv or not re.fullmatch(r"[a-z_]+", field) or field not in values:
            raise ValueError(f"unknown placeholder {{{field}}} (use {', '.join(sorted(values))})")
        out.append(values[field])
    return "".join(out)


def write_atomic(path: Path, text: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def report(engine: Engine, ctx: AlertContext) -> None:
    raw = str(ctx.params.get("path", "")).strip()
    if not raw:
        raise ActionUnavailable("the report action needs a 'path' parameter")
    template = str(ctx.params.get("template", "changed"))
    fmt = ctx.params.get("format")
    stamp = engine.clock.now()
    values = {
        "id": str(ctx.bookmark_id),
        "name": _UNSAFE.sub("_", ctx.name).strip(" ._")[:80] or "bookmark",
        "date": stamp.strftime("%Y%m%d"),
        "datetime": stamp.strftime("%Y%m%d-%H%M%S"),
        "template": template,
    }
    try:
        path = Path(expand(raw, values))
        text, _ = await reports.generate(
            engine, template, fmt=str(fmt) if fmt else None, mark=False
        )
    except (ValueError, LookupError) as exc:  # a configuration mistake: retrying cannot help
        raise ActionUnavailable(str(exc)) from exc
    await asyncio.to_thread(write_atomic, path, text)
