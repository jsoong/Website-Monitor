"""What an alert says about a change, beyond the 200-character summary.

Building it is rendering work, so it follows the pipeline's rule: the event loop hands a small
job to the worker pool and gets strings back. ``build_content`` (the worker half) reads blobs
only. ``load_content`` (the engine half) gathers the rows the job needs.

Privacy: a ``private`` alert never calls any of this (see ``alert_vars``), so the page content
is not even read.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pagewatch.engine.actions.base import AlertContext
from pagewatch.engine.actions.htmlmail import changes_fragment, inline_marks
from pagewatch.engine.bookmarks import NotFoundError
from pagewatch.engine.config import resolve, source_options
from pagewatch.engine.pipeline import sources
from pagewatch.engine.pipeline.diff import DiffResult, diff_blocks
from pagewatch.engine.pipeline.extract import blocks_from_json
from pagewatch.engine.pipeline.render import render_marks
from pagewatch.engine.store import repo
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.models import FilterConfig, HighlightMode

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

MAX_CHANGED_BLOCKS = 200  # shown in a message; the rest is counted ("... and 40 more")
MAX_FULL_HTML = 600_000  # a highlighted page bigger than this is replaced by the changes only
MAX_TEXT = 200_000  # characters of added/removed text handed to templates


@dataclass(slots=True)
class ContentJob:
    blob_root: str
    url: str
    new_blocks_hash: str
    old_blocks_hash: str | None
    diff_hash: str | None
    new_raw_hash: str | None
    content_type: str
    source_type: str
    source_cfg: dict[str, Any]
    filter_cfg: dict[str, Any]
    highlight_mode: str
    want_full: bool = False
    max_blocks: int = MAX_CHANGED_BLOCKS


@dataclass(slots=True)
class ContentResult:
    added_text: str = ""
    removed_text: str = ""
    changes_text: str = ""  # the text view: "+ added", "- removed", "~ edited [-x-]{+y+}"
    changes_html: str = ""  # email-safe table of the changed blocks
    changes_hidden: int = 0  # changed blocks that did not fit in changes_html
    full_html: str | None = None  # the highlighted page, email-safe; None if not available
    diff: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    degraded: bool = False
    new_text: str = ""
    old_text: str = ""
    overlay_hash: str | None = None  # a screenshot change: the boxed-regions picture


def build_content(job: ContentJob) -> ContentResult:
    """Worker entry point: blobs in, strings out."""
    from pagewatch.engine.pipeline import viewer

    store = BlobStore(Path(job.blob_root))
    fcfg = FilterConfig.model_validate(job.filter_cfg)
    new_blocks = blocks_from_json(store.get_json(job.new_blocks_hash))
    old_blocks = (
        blocks_from_json(store.get_json(job.old_blocks_hash)) if job.old_blocks_hash else []
    )
    new_texts = [b.text for b in new_blocks]
    old_texts = [b.text for b in old_blocks]

    overlay: str | None = None
    diff: DiffResult | None = None
    if job.diff_hash:
        payload = store.get_json(job.diff_hash)
        if payload.get("type") == "screenshot":
            overlay = payload.get("overlay")  # the text diff is computed below
        else:
            diff = DiffResult.from_json(payload)
    if diff is None:
        mode = HighlightMode(job.highlight_mode)
        diff = diff_blocks(
            old_texts, new_texts, ignore_case=fcfg.special.ignore_case,
            detect_moves=mode is not HighlightMode.EXACT, table=mode is HighlightMode.TABLE,
        )  # fmt: skip

    html, hidden = changes_fragment(diff.ops, old_texts, new_texts, max_blocks=job.max_blocks)
    result = ContentResult(
        added_text=diff.added_text(new_texts)[:MAX_TEXT],
        removed_text=diff.removed_text(old_texts)[:MAX_TEXT],
        changes_text="\n".join(render_marks(diff, old_texts, new_texts)),
        changes_html=html,
        changes_hidden=hidden,
        diff=diff.to_json(),
        stats=dict(diff.stats),
        degraded=diff.degraded,
        new_text="\n".join(new_texts),
        old_text="\n".join(old_texts),
        overlay_hash=overlay,
    )
    if job.want_full and job.new_raw_hash:
        raw, ctype = sources.view_bytes(
            store.get(job.new_raw_hash), job.content_type, job.url, job.source_type,
            job.source_cfg,
        )  # fmt: skip
        body = viewer.render_highlight(
            raw, ctype, job.url, new_blocks, old_texts, diff,
            nfkc=fcfg.special.normalize_unicode, ignore_options=fcfg.special.ignore_options,
            allow_remote=False,
        )  # fmt: skip
        if body is not None:
            full = inline_marks(body)
            if len(full) <= MAX_FULL_HTML:
                result.full_html = full
    return result


# -- engine side ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChangeContent:
    """A change's content plus the stored versions it came from."""

    result: ContentResult
    new_raw_hash: str | None
    old_raw_hash: str | None
    content_type: str
    screenshot_hash: str | None
    old_screenshot_hash: str | None
    url: str


def load_change_rows(conn: sqlite3.Connection, change_id: int) -> tuple[Any, Any, Any, Any]:
    change = repo.change_get(conn, change_id)
    if change is None:
        return None, None, None, None
    return (
        change,
        repo.bookmark_get(conn, change["bookmark_id"]),
        repo.version_get(conn, change["old_version_id"]) if change["old_version_id"] else None,
        repo.version_get(conn, change["new_version_id"]),
    )


async def load_content(engine: Engine, change_id: int, *, want_full: bool = False) -> ChangeContent:
    """Read the change, run ``build_content`` in the pool. ``NotFoundError`` if it is gone."""
    change, row, old, new = await engine.db.read(lambda c: load_change_rows(c, change_id))
    if change is None or row is None or new is None:
        raise NotFoundError(f"change {change_id} no longer exists")
    resolved = resolve(row, engine.folders, engine.settings)
    result = await engine.pool.run(
        build_content,
        ContentJob(
            blob_root=str(engine.data_dir.blobs_dir),
            url=row["url"],
            new_blocks_hash=new["blocks_hash"],
            old_blocks_hash=old["blocks_hash"] if old is not None else None,
            diff_hash=change["diff_hash"],
            new_raw_hash=new["raw_hash"],
            content_type=new["content_type"] or "",
            source_type=row["source_type"],
            source_cfg=source_options(resolved.fetch),
            filter_cfg=resolved.filter.model_dump(mode="json"),
            highlight_mode=row["highlight_mode"],
            want_full=want_full,
        ),
    )
    return ChangeContent(
        result=result,
        new_raw_hash=new["raw_hash"],
        old_raw_hash=old["raw_hash"] if old is not None else None,
        content_type=new["content_type"] or "",
        screenshot_hash=new["screenshot_hash"],
        old_screenshot_hash=old["screenshot_hash"] if old is not None else None,
        url=row["url"],
    )


def synthetic_content(ctx: AlertContext) -> ChangeContent:
    """What a *test* notification carries (there is no stored change behind it)."""
    text = ctx.summary or "This is a test notification from PageWatch."
    from pagewatch.engine.actions.htmlmail import changes_fragment

    html, _ = changes_fragment([{"t": "ins", "new": [0, 0]}], [], [text])
    return ChangeContent(
        result=ContentResult(
            added_text=text, changes_text=f"+ {text}", changes_html=html,
            stats={"added_words": len(text.split()), "removed_words": 0, "changed_blocks": 1},
            new_text=text,
        ),
        new_raw_hash=None, old_raw_hash=None, content_type="text/plain",
        screenshot_hash=None, old_screenshot_hash=None, url=ctx.url,
    )  # fmt: skip


async def fetch_content(
    engine: Engine, ctx: AlertContext, *, want_full: bool = False
) -> ChangeContent | None:
    """The change's content, or ``None`` for a private alert (whose page content is not read)."""
    if ctx.private:
        return None
    if ctx.change_id <= 0:
        return synthetic_content(ctx)
    return await load_content(engine, ctx.change_id, want_full=want_full)


# -- template variables --------------------------------------------------------------------


def alert_vars(ctx: AlertContext, content: ContentResult | None = None) -> dict[str, Any]:
    """The Jinja2 context for emails and webhooks: ``bookmark`` and ``change``. A private alert
    gets the name and the ids and nothing else, whatever the template asks for."""
    if ctx.private or content is None:
        content = ContentResult()
    blank = ctx.private
    stats = {} if blank else dict(content.stats)
    return {
        "bookmark": {
            "id": ctx.bookmark_id,
            "name": ctx.name,
            "url": "" if blank else ctx.url,
            "info1": "" if blank else (ctx.info[0] or ""),
            "info2": "" if blank else (ctx.info[1] or ""),
            "info3": "" if blank else (ctx.info[2] or ""),
        },
        "change": {
            "id": ctx.change_id,
            "detected_at": ctx.detected_at,
            "summary": "" if blank else (ctx.summary or ""),
            "added_text": content.added_text,
            "removed_text": content.removed_text,
            "added_words": None if blank else ctx.added_words,
            "removed_words": None if blank else ctx.removed_words,
            "changed_blocks": None if blank else ctx.changed_blocks,
            "checks_accumulated": ctx.checks_accumulated,
            "keyword_hits": [] if blank else list(ctx.keyword_hits),
            "stats": stats,
        },
        "private": blank,
    }
