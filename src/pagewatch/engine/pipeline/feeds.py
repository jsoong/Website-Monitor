"""RSS and Atom feeds to HTML: one block per entry, keyed by entry id or link.

Each entry becomes a single ``<li>`` (title, then summary), so a new entry is one inserted
block and an edited entry is one replaced block. Timestamps are left out on purpose: many feeds
bump ``updated`` on every fetch, which would be a change that is not one.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from typing import Any

from pagewatch.engine.pipeline.documents import DocumentError
from pagewatch.models import FeedOptions

Warn = Callable[[str], None]
MAX_SUMMARY_CHARS = 2000


def _plain(markup: str) -> str:
    """Summary markup to text."""
    if "<" not in markup:
        return " ".join(markup.split())
    from lxml import html as lhtml

    try:
        text = lhtml.fromstring(f"<div>{markup}</div>").text_content()
    except Exception:
        text = markup
    return " ".join(text.split())


def entry_key(entry: Any) -> str:
    return str(entry.get("id") or entry.get("link") or entry.get("title") or "")


def feed_entries(data: bytes, opts: FeedOptions, warn: Warn | None = None) -> tuple[Any, list[Any]]:
    """The parsed feed and its de-duplicated entries (feed order), capped at ``max_entries``."""
    warn = warn or (lambda _m: None)
    import feedparser

    parsed = feedparser.parse(data)
    entries = list(parsed.entries)
    if not entries and not parsed.feed.get("title"):
        reason = f": {parsed.bozo_exception}" if getattr(parsed, "bozo", 0) else ""
        raise DocumentError(f"not an RSS or Atom feed{reason}"[:300])
    seen: set[str] = set()
    kept: list[Any] = []
    for entry in entries:
        key = entry_key(entry)
        if key and key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    if len(kept) > opts.max_entries:
        warn(f"feed read up to {opts.max_entries} entries")
        kept = kept[: opts.max_entries]
    return parsed, kept


def feed_to_html(data: bytes, opts: FeedOptions, warn: Warn | None = None) -> str:
    parsed, entries = feed_entries(data, opts, warn)
    items: list[str] = []
    for entry in entries:
        title = _plain(str(entry.get("title") or ""))
        summary = ""
        if opts.summary:
            raw = entry.get("summary") or ""
            if not raw and entry.get("content"):
                raw = entry["content"][0].get("value", "")
            summary = _plain(str(raw))[:MAX_SUMMARY_CHARS]
        text = f"{title} — {summary}" if (title and summary) else (title or summary)
        if not text:
            continue
        link = str(entry.get("link") or "")
        label = escape(text)
        inner = f'<a href="{escape(link, quote=True)}">{label}</a>' if link else label
        for enc in entry.get("enclosures") or []:
            href = str(enc.get("href") or "")
            if href:
                inner += f' <a href="{escape(href, quote=True)}"></a>'
        items.append(f'<li data-key="{escape(entry_key(entry), quote=True)}">{inner}</li>')
    heading = _plain(str(parsed.feed.get("title") or ""))
    head = f"<h1>{escape(heading)}</h1>" if heading else ""
    return f"<!DOCTYPE html><html><body>{head}<ul>{''.join(items)}</ul></body></html>"
