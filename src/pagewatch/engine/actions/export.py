"""The ``export`` action: write a change to a file.

Formats (spec): ``html`` (the highlighted page as one self-contained document), ``html_assets``
(the same plus the page's images and stylesheets saved beside it: the "local website archive"
partial), ``text`` (the changes as plain text) and ``diff_json`` (the stored diff plus the
change's facts).

The path is a ``{var}`` template. Substituted values are made safe path components, and
``{datetime}`` / ``{date:...}`` come from the change's detection time, not from now, so running a
job twice after a crash writes the same file. Relative paths are relative to the data folder's
``exports\\`` directory. Writes are atomic: a reader sees the old file or the new one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from pagewatch.engine.actions.assets import localize_assets, stylesheet_links
from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import (
    detected_at,
    local_time,
    parse_params,
    template_values,
    write_atomic,
)
from pagewatch.engine.actions.content import load_change_rows, load_content
from pagewatch.engine.actions.templating import TemplateError, expand
from pagewatch.engine.changes import render_change_raw
from pagewatch.engine.logs import get_logger
from pagewatch.models import ExportParams

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.export")


def resolve_path(engine: Engine, ctx: AlertContext, template: str, what: str = "export") -> Path:
    try:
        text = expand(
            template,
            template_values(ctx),
            when=detected_at(ctx),
            to_local=engine.zone.to_local,
            safe=True,
        )
    except TemplateError as exc:
        raise ActionConfigError(f"{what} path: {exc}") from exc
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = engine.data_dir.root / "exports" / path
    if path.name in ("", ".", ".."):
        raise ActionConfigError(f"{what} path {template!r} does not name a file")
    return path


def assets_folder(path: Path) -> Path:
    return path.with_name(path.stem + "_files")


async def _page_stylesheets(engine: Engine, ctx: AlertContext) -> list[str]:
    """The stylesheets the stored page links to (the viewer's document has only the body)."""
    _change, row, _old, new = await engine.db.read(lambda c: load_change_rows(c, ctx.change_id))
    if row is None or new is None or not new["raw_hash"]:
        return []
    raw = await asyncio.to_thread(engine.blobs.get, new["raw_hash"])
    return await asyncio.to_thread(stylesheet_links, raw, new["content_type"] or "", row["url"])


async def export(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(ExportParams, ctx.params, "export")
    path = resolve_path(engine, ctx, params.path)
    when = local_time(engine, detected_at(ctx))

    if params.format in ("html", "html_assets"):
        view = "highlight" if params.highlight else "new"
        remote = params.format == "html_assets"
        rendered = await render_change_raw(
            engine, ctx.change_id, view, allow_remote=remote, context=None
        )
        html = rendered.html
        if remote:
            html, report = await localize_assets(
                html, ctx.url, assets_folder(path), assets_folder(path).name,
                await _page_stylesheets(engine, ctx),
            )  # fmt: skip
            for skipped in report.skipped:
                log.warning("export_asset_skipped", change=ctx.change_id, reason=skipped)
        data = html.encode("utf-8")
    else:
        content = await load_content(engine, ctx.change_id)
        result = content.result
        if params.format == "text":
            stats = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in result.stats.items())
            head = [ctx.name, ctx.url, f"Detected {when:%Y-%m-%d %H:%M:%S}"]
            if stats:
                head.append(stats)
            data = ("\n".join(head) + "\n\n" + result.changes_text + "\n").encode("utf-8")
        else:
            payload = {
                "bookmark": {"id": ctx.bookmark_id, "name": ctx.name, "url": ctx.url},
                "change": {
                    "id": ctx.change_id,
                    "detected_at": ctx.detected_at,
                    "summary": ctx.summary,
                    "checks_accumulated": ctx.checks_accumulated,
                    "keyword_hits": ctx.keyword_hits,
                },
                "stats": result.stats,
                "degraded": result.degraded,
                "added_text": result.added_text,
                "removed_text": result.removed_text,
                "diff": result.diff,
            }
            data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    await asyncio.to_thread(write_atomic, path, data)
    log.info("export_written", bookmark=ctx.bookmark_id, change=ctx.change_id, path=str(path))
