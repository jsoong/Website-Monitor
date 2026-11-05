"""The API a PageWatch plugin sees (spec: Extensibility → Plugin hooks).

A plugin is a Python module in ``<data folder>/plugins/`` that defines any of these functions:

* ``before_check(ctx)``: before the fetch. ``return False`` skips the check; change
  ``ctx.request.url``, ``.headers`` or ``.body`` to change the request.
* ``merge_pages(ctx, page)``: after the fetch. Return more HTML (``str``, ``bytes`` or a list of
  them) to append to the page.
* ``preprocess(ctx, html) -> str``: before block extraction. Rewrite the content.
* ``compare(ctx, old, new) -> Verdict | None``: after the diff. ``Verdict.change()``,
  ``Verdict.no_change()`` or ``None`` to defer. ``old`` and ``new`` are the page's text blocks and
  ``ctx.diff`` has the statistics.
* ``check_keywords(ctx, added_text) -> bool | None``: custom keyword logic, OR-ed with the
  built-in keywords; ``None`` defers.
* ``on_change(ctx, change)``: after an alert was raised.
* ``after_check(ctx, run)``: after every check.

Plugins are trusted local code. A hook that raises is logged and treated as absent; a hook slower
than 5 seconds adds a warning to the Check log. Hooks are plain (synchronous) functions.

``ctx`` gives a read-only ``bookmark`` (plus ``set_property`` for the info fields, the note and
the name), a rate-limited ``http_get``, ``log``, a per-bookmark persistent ``state`` dict (JSON
values), ``option()`` to read ``key=value`` settings from the bookmark's info / note fields,
``now()``, ``notify()`` and the same text, regex and HTML helpers the pipeline uses
(``ctx.helpers``). See ``pagewatch.data.plugins`` for four worked examples.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

HOOKS = (
    "before_check",
    "merge_pages",
    "preprocess",
    "compare",
    "check_keywords",
    "on_change",
    "after_check",
)
SETTABLE = frozenset({"info1", "info2", "info3", "note", "name"})
HTTP_GAP_S = 2.0  # minimum seconds between two ``http_get`` calls to one host
HTTP_MAX_CALLS = 10  # per hook call
HTTP_MAX_BYTES = 5 * 1024 * 1024

_OPTION = re.compile(r"""(\w+)\s*=\s*("[^"]*"|'[^']*'|[^\s;]+)""")


class PluginError(RuntimeError):
    """Raised to a plugin that misuses the API (an unknown property, too many requests...)."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What ``compare`` returns: force a change, or force no change. ``None`` defers."""

    alert: bool
    reason: str | None = None

    @classmethod
    def change(cls, reason: str | None = None) -> Verdict:
        """Treat this as a change worth alerting on, whatever the other rules said."""
        return cls(True, reason)

    @classmethod
    def no_change(cls, reason: str = "plugin") -> Verdict:
        """Suppress the alert (the new version is still stored); ``reason`` is recorded."""
        return cls(False, reason)


@dataclass(frozen=True, slots=True)
class BookmarkView:
    """A bookmark as a plugin sees it. Read-only: use ``ctx.set_property``."""

    id: int
    name: str
    url: str
    folder_id: int | None
    source_type: str
    check_method: str
    info1: str | None
    info2: str | None
    info3: str | None
    note: str | None
    plugin: str | None
    status: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BookmarkView:
        return cls(**{f: data.get(f) for f in cls.__dataclass_fields__})


@dataclass(slots=True)
class CheckRequest:
    """``before_check`` may change these; the check then fetches the changed request."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None  # a POST body (setting one makes the request a POST)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """What ``merge_pages`` receives."""

    final_url: str
    status: int | None
    headers: dict[str, str]
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        from pagewatch.engine.pipeline.extract import decode_body

        return decode_body(self.body, self.content_type)


@dataclass(frozen=True, slots=True)
class ChangeInfo:
    """What ``on_change`` receives."""

    id: int
    bookmark_id: int
    detected_at: str
    summary: str | None
    added_words: int | None
    removed_words: int | None
    changed_blocks: int | None
    checks_accumulated: int
    keyword_hits: list[str]


@dataclass(frozen=True, slots=True)
class RunInfo:
    """What ``after_check`` receives."""

    bookmark_id: int
    outcome: str  # first | unchanged | changed | suppressed | error | skipped
    reason: str | None
    method: str | None
    duration_ms: int
    changed: bool  # the bookmark's content changed (even if the gate suppressed the alert)
    alerted: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    url: str
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        from pagewatch.engine.pipeline.extract import decode_body

        return decode_body(self.content, self.headers.get("content-type", ""))

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class Helpers:
    """The text, regex and HTML helpers the pipeline uses, for plugins to reuse."""

    @staticmethod
    def normalize(text: str) -> str:
        """Collapse whitespace, NFKC, drop invisible characters (what blocks are made of)."""
        from pagewatch.engine.pipeline.extract import normalize_text

        return normalize_text(text)

    @staticmethod
    def parse_html(markup: str) -> Any | None:
        """An lxml tree (``None`` for an empty document)."""
        from pagewatch.engine.pipeline.extract import parse_html

        return parse_html(markup)

    @staticmethod
    def select(root: Any, css: str) -> list[Any]:
        """Elements matching a CSS selector, in document order."""
        from pagewatch.engine.pipeline.filters import select

        return select(root, "css", css)

    @staticmethod
    def text_of(element: Any) -> str:
        return Helpers.normalize("".join(element.itertext()))

    @staticmethod
    def pattern(pattern: str, kind: str = "regex", ignore_case: bool = True) -> re.Pattern[str]:
        """Compile a ``literal`` / ``wildcard`` (``*``, ``?``) / ``regex`` pattern like a filter."""
        from pagewatch.engine.pipeline.filters import text_pattern
        from pagewatch.models import FilterRule

        return text_pattern(
            FilterRule(type="text", pattern=pattern, pattern_kind=kind),  # type: ignore[arg-type]
            ignore_case,
        )

    @staticmethod
    def numbers(text: str) -> list[float]:
        """Every number in ``text`` (``$1,299.00`` is 1299.0, ``1.099,50`` is 1099.5)."""
        from pagewatch.engine.pipeline.keywords import parse_number

        out: list[float] = []
        for m in re.finditer(r"\d[\d.,]*", text):
            value = parse_number(m.group(0).rstrip(".,"))
            if value is not None:
                out.append(value)
        return out


@dataclass(slots=True)
class PluginContext:
    """Passed as the first argument of every hook."""

    bookmark: BookmarkView
    state: dict[str, Any]
    user_agent: str = ""
    request: CheckRequest | None = None  # before_check only
    diff: dict[str, int] = field(default_factory=dict)  # compare / check_keywords: the statistics
    helpers: ClassVar[type[Helpers]] = Helpers
    _now: datetime | None = field(default=None, repr=False)
    _log: Callable[[str, str], None] | None = field(default=None, repr=False)
    _props: dict[str, str | None] = field(default_factory=dict, repr=False)
    _alerts: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _last_http: dict[str, float] = field(default_factory=dict, repr=False)
    _http_calls: int = field(default=0, repr=False)

    # -- the bookmark ---------------------------------------------------------------------

    def set_property(self, name: str, value: str | None) -> None:
        """Change ``info1``, ``info2``, ``info3``, ``note`` or ``name`` of this bookmark (applied
        when the check finishes)."""
        if name not in SETTABLE:
            raise PluginError(f"cannot set {name!r}; use one of {', '.join(sorted(SETTABLE))}")
        if name == "name" and not value:
            raise PluginError("a bookmark needs a name")
        self._props[name] = value

    def option(self, name: str, default: str | None = None) -> str | None:
        """A ``name=value`` setting written in the bookmark's info fields or note, e.g. an
        ``info1`` of ``min=100 max=250``. Quote values that contain spaces."""
        return self.options.get(name, default)

    @property
    def options(self) -> dict[str, str]:
        found: dict[str, str] = {}
        b = self.bookmark
        for text in (b.info1, b.info2, b.info3, b.note):
            for key, raw in _OPTION.findall(text or ""):
                found.setdefault(key, raw[1:-1] if raw[:1] in "\"'" else raw)
        return found

    # -- the world ------------------------------------------------------------------------

    def now(self) -> datetime:
        """The engine's clock (not necessarily the wall clock)."""
        from datetime import UTC

        return self._now or datetime.now(UTC)

    def log(self, message: str, level: str = "info") -> None:
        if self._log is not None:
            self._log(level, str(message))

    def notify(self, title: str, body: str = "") -> None:
        """Show a notification for this bookmark (a toast), after the hook returns."""
        self._alerts.append((str(title), str(body)))

    def http_get(
        self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0
    ) -> HttpResponse:
        """A rate-limited GET (``HTTP_GAP_S`` between requests to a host, at most
        ``HTTP_MAX_CALLS`` per hook call). Raises ``PluginError`` on a transport failure."""
        import httpx

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PluginError(f"http_get needs an http(s) URL, not {url!r}")
        if self._http_calls >= HTTP_MAX_CALLS:
            raise PluginError(f"a hook may make at most {HTTP_MAX_CALLS} http_get calls")
        self._http_calls += 1
        host = parts.hostname.lower()
        wait = self._last_http.get(host, 0.0) + HTTP_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        merged = {"User-Agent": self.user_agent or "PageWatch", **(headers or {})}
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                resp = client.get(url, headers=merged)
        except httpx.HTTPError as exc:
            raise PluginError(f"http_get {url}: {exc}") from exc
        finally:
            self._last_http[host] = time.monotonic()
        content = resp.content[:HTTP_MAX_BYTES]
        headers_out = {k.lower(): v for k, v in resp.headers.items()}
        return HttpResponse(resp.status_code, str(resp.url), headers_out, content)
