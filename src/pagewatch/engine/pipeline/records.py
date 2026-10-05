"""Records sources: a JSON or CSV feed whose rows are watched record by record.

Each row becomes one block keyed by its ID, so the normal diff, gate, history and viewer
apply unchanged, and the alert can name what was added or changed instead of showing a text
diff (spec: Records sources). Three small pieces live here, none of which needs a dependency:

* a JSONPath subset (``$``, ``.key``, ``['key']``, ``[n]``, ``[*]``, ``.*``, ``..key``) that
  locates the row array,
* a row-filter language (``borough in [MN, BK] and lottery_status = Active``),
* record events: ``new`` (an unseen ID), ``changed`` (a watched field differs) and ``removed``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from pagewatch.engine.pipeline.extract import Block, normalize_text
from pagewatch.models import RecordsConfig

RECORD_KIND = "record"
PATH_PREFIX = "record:"
MAX_ROWS = 200_000


class RecordsSyntaxError(ValueError):
    """A JSONPath or row filter that cannot be parsed (rejected when the config is saved)."""


class RecordsError(ValueError):
    """The fetched content does not have the configured shape (a failed check, not a change)."""


# -- JSONPath subset --------------------------------------------------------------------

_NAME = re.compile(r"[^.\[\]*]+")


@dataclass(frozen=True, slots=True)
class Step:
    kind: str  # key | index | wildcard | descend
    value: str | int | None = None


def parse_path(path: str) -> list[Step]:
    """Parse a JSONPath into steps. Raises ``RecordsSyntaxError``."""
    text = path.strip()
    if not text.startswith("$"):
        raise RecordsSyntaxError(f"JSONPath {path!r} must start with '$'")
    steps: list[Step] = []
    i = 1
    n = len(text)
    while i < n:
        if text.startswith("..", i):
            i += 2
            m = _NAME.match(text, i)
            if m is None:
                raise RecordsSyntaxError(f"JSONPath {path!r}: expected a name after '..'")
            steps.append(Step("descend", m.group()))
            i = m.end()
        elif text[i] == ".":
            i += 1
            if text.startswith("*", i):
                steps.append(Step("wildcard"))
                i += 1
                continue
            m = _NAME.match(text, i)
            if m is None:
                raise RecordsSyntaxError(f"JSONPath {path!r}: expected a name after '.'")
            steps.append(Step("key", m.group()))
            i = m.end()
        elif text[i] == "[":
            end = text.find("]", i)
            if end < 0:
                raise RecordsSyntaxError(f"JSONPath {path!r}: unclosed '['")
            inner = text[i + 1 : end].strip()
            i = end + 1
            if inner == "*":
                steps.append(Step("wildcard"))
            elif len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                steps.append(Step("key", inner[1:-1]))
            else:
                try:
                    steps.append(Step("index", int(inner)))
                except ValueError as exc:
                    raise RecordsSyntaxError(
                        f"JSONPath {path!r}: unsupported selector [{inner}]"
                    ) from exc
        else:
            raise RecordsSyntaxError(f"JSONPath {path!r}: unexpected {text[i]!r} at {i}")
    return steps


def _descend(node: Any, name: str) -> Iterator[Any]:
    if isinstance(node, dict):
        if name in node:
            yield node[name]
        for child in node.values():
            yield from _descend(child, name)
    elif isinstance(node, list):
        for child in node:
            yield from _descend(child, name)


def evaluate_path(steps: Sequence[Step], root: Any) -> list[Any]:
    """Every node the path selects, in document order."""
    nodes = [root]
    for step in steps:
        out: list[Any] = []
        for node in nodes:
            if step.kind == "key":
                if isinstance(node, dict) and step.value in node:
                    out.append(node[step.value])
            elif step.kind == "index":
                assert isinstance(step.value, int)
                if isinstance(node, list) and -len(node) <= step.value < len(node):
                    out.append(node[step.value])
            elif step.kind == "wildcard":
                if isinstance(node, list):
                    out.extend(node)
                elif isinstance(node, dict):
                    out.extend(node.values())
            elif step.kind == "descend":
                assert isinstance(step.value, str)
                out.extend(_descend(node, step.value))
        nodes = out
    return nodes


# -- row filter -------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<op>!=|==|<=|>=|=|<|>)
      | (?P<lb>\[)
      | (?P<rb>\])
      | (?P<comma>,)
      | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<word>[^\s,\[\]=<>!"']+)
    )""",
    re.VERBOSE,
)
_KEYWORDS = {"and", "or", "not", "in", "contains", "startswith", "endswith"}

RowFilter = Callable[[dict[str, Any]], bool]


def _tokenize(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if m is None or m.end() == pos:
            raise RecordsSyntaxError(f"row filter: unexpected {expr[pos : pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        text = m.group(kind)
        if kind == "str":
            body = text[1:-1]
            text = re.sub(r"\\(.)", r"\1", body)
        out.append((kind, text))
    return out


def get_field(row: Any, name: str) -> Any:
    """``name`` may be dotted (``address.borough``); a literal key with dots wins."""
    if isinstance(row, dict) and name in row:
        return row[name]
    cur = row
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def _same(a: Any, b: str) -> bool:
    if a is None:
        return False
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return scalar_text(a).casefold() == b.casefold()


def _compare(op: str, a: Any, b: str) -> bool:
    if op in ("=", "=="):
        return _same(a, b)
    if op == "!=":
        return not _same(a, b)
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return {"<": na < nb, "<=": na <= nb, ">": na > nb, ">=": na >= nb}[op]
    if a is None:
        return False
    sa, sb = scalar_text(a).casefold(), b.casefold()
    return {"<": sa < sb, "<=": sa <= sb, ">": sa > sb, ">=": sa >= sb}[op]


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise RecordsSyntaxError("row filter: unexpected end")
        self.i += 1
        return tok

    def _is_kw(self, word: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "word" and tok[1].lower() == word

    def parse(self) -> RowFilter:
        fn = self.or_expr()
        if self.peek() is not None:
            raise RecordsSyntaxError(f"row filter: unexpected {self.peek()[1]!r}")  # type: ignore[index]
        return fn

    def or_expr(self) -> RowFilter:
        parts = [self.and_expr()]
        while self._is_kw("or"):
            self.take()
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else (lambda r: any(p(r) for p in parts))

    def and_expr(self) -> RowFilter:
        parts = [self.term()]
        while self._is_kw("and"):
            self.take()
            parts.append(self.term())
        return parts[0] if len(parts) == 1 else (lambda r: all(p(r) for p in parts))

    def value(self) -> str:
        kind, text = self.take()
        if kind not in ("word", "str"):
            raise RecordsSyntaxError(f"row filter: expected a value, found {text!r}")
        return text

    def term(self) -> RowFilter:
        negate = False
        while self._is_kw("not"):
            self.take()
            negate = not negate
        kind, name = self.take()
        if kind != "word" or name.lower() in _KEYWORDS:
            raise RecordsSyntaxError(f"row filter: expected a field name, found {name!r}")
        nxt = self.peek()
        if nxt is None:
            raise RecordsSyntaxError(f"row filter: {name!r} needs an operator")
        fn: RowFilter
        if nxt[0] == "op":
            op = self.take()[1]
            val = self.value()

            def fn(r: dict[str, Any], n: str = name, o: str = op, v: str = val) -> bool:
                return _compare(o, get_field(r, n), v)

        elif self._is_kw("in") or (self._is_kw("not") and self._next_is_in()):
            neg = False
            if self._is_kw("not"):
                self.take()
                neg = True
            self.take()  # in
            items = self.list_()

            def fn(r: dict[str, Any], n: str = name, it: list[str] = items, ng: bool = neg) -> bool:
                return any(_same(get_field(r, n), x) for x in it) != ng

        elif nxt[0] == "word" and nxt[1].lower() in ("contains", "startswith", "endswith"):
            word = self.take()[1].lower()
            val = self.value().casefold()

            def fn(r: dict[str, Any], n: str = name, w: str = word, v: str = val) -> bool:
                got = get_field(r, n)
                if got is None:
                    return False
                s = scalar_text(got).casefold()
                return (
                    v in s
                    if w == "contains"
                    else (s.startswith(v) if w == "startswith" else s.endswith(v))
                )
        else:
            raise RecordsSyntaxError(f"row filter: unknown operator {nxt[1]!r} after {name!r}")
        if negate:
            inner = fn
            return lambda r: not inner(r)
        return fn

    def _next_is_in(self) -> bool:
        nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
        return nxt is not None and nxt[0] == "word" and nxt[1].lower() == "in"

    def list_(self) -> list[str]:
        if self.take()[0] != "lb":
            raise RecordsSyntaxError("row filter: 'in' needs a list like [a, b]")
        items: list[str] = []
        while True:
            tok = self.peek()
            if tok is None:
                raise RecordsSyntaxError("row filter: unclosed '['")
            if tok[0] == "rb":
                self.take()
                return items
            items.append(self.value())
            tok = self.peek()
            if tok is not None and tok[0] == "comma":
                self.take()


def parse_row_filter(expr: str) -> RowFilter:
    """Compile a row filter. Raises ``RecordsSyntaxError``."""
    tokens = _tokenize(expr)
    if not tokens:
        raise RecordsSyntaxError("row filter is empty")
    return _Parser(tokens).parse()


# -- rows -------------------------------------------------------------------------------


def scalar_text(value: Any) -> str:
    """How a field value reads inside a record's block text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def detect_format(cfg: RecordsConfig, text: str, content_type: str, url: str) -> str:
    if cfg.format != "auto":
        return cfg.format
    ct = content_type.split(";")[0].strip().lower()
    if "csv" in ct or url.lower().split("?", 1)[0].endswith((".csv", ".tsv")):
        return "csv"
    if "json" in ct or url.lower().split("?", 1)[0].endswith(".json"):
        return "json"
    return "json" if text.lstrip()[:1] in ("{", "[") else "csv"


def load_rows(
    text: str,
    cfg: RecordsConfig,
    *,
    content_type: str = "",
    url: str = "",
    warn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """The rows of a JSON or CSV document (after the path and the row filter).

    Raises ``RecordsError`` when the content does not have the configured shape: an API that
    changed its layout must fail the check, not look like every record was removed.
    """
    warn = warn or (lambda _m: None)
    fmt = detect_format(cfg, text, content_type, url)
    rows: list[Any]
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text), delimiter=cfg.delimiter)
        if not reader.fieldnames:
            raise RecordsError("CSV has no header row")
        if cfg.id_field not in reader.fieldnames:
            raise RecordsError(f"CSV has no column {cfg.id_field!r}")
        rows = []
        for n, r in enumerate(reader):
            if n >= MAX_ROWS:
                warn(f"CSV truncated at {MAX_ROWS} rows")
                break
            rows.append({k: v for k, v in r.items() if k is not None})
    else:
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise RecordsError(f"not valid JSON: {exc}") from exc
        found = evaluate_path(parse_path(cfg.path), doc)
        if not found:
            raise RecordsError(f"JSONPath {cfg.path!r} matched nothing")
        rows = []
        for node in found:
            if isinstance(node, list):
                rows.extend(node)
            elif isinstance(node, dict):
                if all(isinstance(v, dict) for v in node.values()) and node:
                    # {"123": {...}, "456": {...}}: the key is the ID when the row has none
                    for key, value in node.items():
                        rows.append({cfg.id_field: key, **value})
                else:
                    rows.append(node)
            else:
                raise RecordsError(f"JSONPath {cfg.path!r} does not select rows")
        if len(rows) > MAX_ROWS:
            warn(f"records truncated at {MAX_ROWS} rows")
            rows = rows[:MAX_ROWS]
    keep: RowFilter | None = None
    if cfg.filter and cfg.filter.strip():
        keep = parse_row_filter(cfg.filter)
    out: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        if keep is None or keep(row):
            out.append(row)
    if skipped:
        warn(f"{skipped} rows were not objects and were skipped")
    return out


def _natural(rid: str) -> tuple[int, int, str]:
    return (0, int(rid), rid) if rid.isdigit() else (1, 0, rid.casefold())


def record_blocks(
    rows: Sequence[dict[str, Any]],
    cfg: RecordsConfig,
    *,
    nfkc: bool = True,
    warn: Callable[[str], None] | None = None,
) -> list[Block]:
    """One block per record: the ID first, then the watched fields in a stable order.

    Blocks are sorted by ID (numerically when the IDs are numbers), so a feed that reorders
    its rows is not a change. Rows without an ID are skipped; a repeated ID keeps the first row.
    """
    warn = warn or (lambda _m: None)
    seen: dict[str, Block] = {}
    missing = dupes = 0
    for row in rows:
        rid = normalize_text(scalar_text(get_field(row, cfg.id_field)), nfkc=nfkc)
        if not rid:
            missing += 1
            continue
        if rid in seen:
            dupes += 1
            continue
        if cfg.fields:
            names = [f for f in cfg.fields if f != cfg.id_field]
        else:
            names = sorted(k for k in row if k != cfg.id_field)
        parts = [f"{cfg.id_field}: {rid}"]
        parts += [f"{f}: {scalar_text(get_field(row, f))}" for f in names]
        text = normalize_text(" | ".join(parts), nfkc=nfkc)
        seen[rid] = Block(f"{PATH_PREFIX}{rid}", RECORD_KIND, text)
    if missing:
        warn(f"{missing} rows had no {cfg.id_field!r} and were skipped")
    if dupes:
        warn(f"{dupes} rows repeated an ID and were ignored")
    return [seen[k] for k in sorted(seen, key=_natural)]


# -- events -----------------------------------------------------------------------------


@dataclass(slots=True)
class RecordEvents:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def kinds(self) -> set[str]:
        return {k for k in ("new", "changed", "removed") if getattr(self, k)}

    def __bool__(self) -> bool:
        return bool(self.new or self.changed or self.removed)


def is_record_block(block: Block) -> bool:
    return block.kind == RECORD_KIND and block.path.startswith(PATH_PREFIX)


def record_events(
    old: Sequence[Block], new: Sequence[Block], *, ignore_case: bool = True
) -> RecordEvents:
    """``new`` (an unseen ID), ``changed`` (a watched field differs), ``removed``.
    Each list holds record IDs; ``changed`` and ``new`` follow the new version's order."""

    def key(b: Block) -> str:
        return b.text.casefold() if ignore_case else b.text

    before = {b.path: key(b) for b in old if is_record_block(b)}
    ev = RecordEvents()
    seen: set[str] = set()
    for b in new:
        if not is_record_block(b):
            continue
        seen.add(b.path)
        rid = b.path[len(PATH_PREFIX) :]
        if b.path not in before:
            ev.new.append(rid)
        elif before[b.path] != key(b):
            ev.changed.append(rid)
    ev.removed = [p[len(PATH_PREFIX) :] for p in before if p not in seen]
    return ev


def describe_events(ev: RecordEvents, new: Sequence[Block], old: Sequence[Block]) -> str:
    """ "New: lottery_id: 42 | name: Sunset Terrace; Changed (2): ..." naming each record."""
    text_of = {b.path[len(PATH_PREFIX) :]: b.text for b in new if is_record_block(b)}
    old_of = {b.path[len(PATH_PREFIX) :]: b.text for b in old if is_record_block(b)}

    def line(label: str, ids: list[str], source: dict[str, str]) -> str:
        if not ids:
            return ""
        shown = "; ".join(source.get(i, i)[:90] for i in ids[:3])
        more = f" (+{len(ids) - 3} more)" if len(ids) > 3 else ""
        count = f" ({len(ids)})" if len(ids) > 1 else ""
        return f"{label}{count}: {shown}{more}"

    parts = [
        line("New", ev.new, text_of),
        line("Changed", ev.changed, text_of),
        line("Removed", ev.removed, old_of),
    ]
    return " | ".join(p for p in parts if p)
