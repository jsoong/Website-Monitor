"""Import and export (spec: Import, export, reports and backup).

Import reads browser bookmarks (Netscape HTML), a plain URL list, CSV or XLSX with the columns
``name, url, folder, interval, keywords``, or a PageWatch export, and creates bookmarks through the
same service the API uses. Rows whose normalised URL already exists (in the database or earlier in
the same file) are skipped, and a bad row is reported without stopping the others.

Export writes the whole configuration as versioned JSON (stored overrides only, never secrets),
an OPML outline of the feed bookmarks, or a CSV list that the importer reads back.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr

from pydantic import ValidationError

from pagewatch import __version__
from pagewatch.engine import bookmarks as svc
from pagewatch.engine.clock import iso
from pagewatch.engine.config import resolve
from pagewatch.engine.store import repo
from pagewatch.engine.urls import normalize_url
from pagewatch.models import (
    EXPORT_VERSION,
    MIN_INTERVAL_S,
    BookmarkIn,
    ExportBookmark,
    ExportDocument,
    ExportFolder,
    ImportFormat,
    ImportRequest,
    ImportResult,
    ImportRowError,
    dumps,
    loads,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

_INTERVAL = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "file://")
_BARE_HOST = re.compile(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/\S*)?$")
_FOLDER_SEP = re.compile(r"\s*[/>\\]\s*")
_FEED_TYPES = ("rss", "atom")
_COLUMNS = ("name", "url", "folder", "interval", "keywords")
BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"  # UTF-8 byte-order mark: Excel needs it to read UTF-8 CSV


class ImportError_(ValueError):
    """The file as a whole cannot be read (wrong format, corrupt workbook, ...)."""


@dataclass(slots=True)
class ImportItem:
    row: int
    url: str
    name: str | None = None
    folder: list[str] = field(default_factory=list)
    interval_s: int | None = None
    keywords: str | None = None
    full: ExportBookmark | None = None  # a PageWatch export: the whole configuration
    folder_ref: int | None = None  # ... and the id of its folder in that export
    error: str | None = None  # the row could not be read (reported, the others still import)


def parse_interval(text: str) -> int:
    """``90``/``90s`` seconds, ``15m``, ``2h``, ``1d``, ``1w``; at least a minute."""
    m = _INTERVAL.match(text)
    if not m:
        raise ValueError(f"cannot read interval {text!r} (examples: 15m, 2h, 1d)")
    seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
    if seconds < MIN_INTERVAL_S:
        raise ValueError(f"interval {text!r} is under the 1 minute minimum")
    return seconds


def folder_path(text: str | None) -> list[str]:
    """``News/Tech`` or ``News > Tech`` -> ``["News", "Tech"]``."""
    if not text:
        return []
    return [p.strip() for p in _FOLDER_SEP.split(text.strip()) if p.strip()]


# -- parsers ----------------------------------------------------------------------------


class _Netscape(HTMLParser):
    """Browser bookmark exports: nested ``<DL>`` lists, ``<H3>`` folder names, ``<A HREF>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[ImportItem] = []
        self._stack: list[str | None] = []
        self._pending: str | None = None
        self._capture: str | None = None  # "h3" | "a"
        self._text: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "dl":
            self._stack.append(self._pending)
            self._pending = None
        elif tag in ("h3", "a"):
            self._capture, self._text = tag, []
            if tag == "a":
                self._href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "dl":
            if self._stack:
                self._stack.pop()
        elif tag == self._capture:
            text = " ".join("".join(self._text).split())
            if tag == "h3":
                self._pending = text or None
            elif self._href:
                href = self._href.strip()
                if href.lower().startswith(_SCHEMES):
                    path = [n for n in self._stack if n]
                    self.items.append(ImportItem(len(self.items) + 1, href, text or None, path))
            self._capture, self._href = None, None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._text.append(data)


def parse_netscape(text: str) -> list[ImportItem]:
    parser = _Netscape()
    parser.feed(text)
    parser.close()
    return parser.items


def parse_urls(text: str) -> list[ImportItem]:
    items: list[ImportItem] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        m = re.search(r"(?:https?|ftps?|file)://\S+", line, re.IGNORECASE)
        if m:
            name = (line[: m.start()] + " " + line[m.end() :]).strip(" \t-,;|")
            items.append(ImportItem(number, m.group(0), name or None))
        elif _BARE_HOST.match(line):
            items.append(ImportItem(number, "https://" + line))
        else:
            items.append(ImportItem(number, line))  # reported as a bad URL by the importer
    return items


def _rows_to_items(rows: Iterable[tuple[int, dict[str, str]]]) -> list[ImportItem]:
    items: list[ImportItem] = []
    for number, row in rows:
        if not any(v.strip() for v in row.values()):
            continue  # a blank row
        item = ImportItem(
            number,
            row.get("url", "").strip(),
            row.get("name", "").strip() or None,
            folder_path(row.get("folder")),
            None,
            row.get("keywords", "").strip().replace("\r\n", "\n") or None,
        )
        raw_interval = row.get("interval", "").strip()
        if raw_interval:
            try:
                item.interval_s = parse_interval(raw_interval)
            except ValueError as exc:
                item.error = str(exc)
        items.append(item)
    return items


def _normal_header(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _rows_from_table(
    header: list[object], body: Iterable[list[object]], first_row: int
) -> list[tuple[int, dict[str, str]]]:
    index = {_normal_header(h): i for i, h in enumerate(header) if h not in (None, "")}
    if "url" not in index:
        raise ImportError_("the first row must name the columns; a 'url' column is required")
    out: list[tuple[int, dict[str, str]]] = []
    for offset, cells in enumerate(body):
        row = {
            col: ("" if index[col] >= len(cells) or cells[index[col]] is None
                  else str(cells[index[col]]))
            for col in _COLUMNS if col in index
        }  # fmt: skip
        out.append((first_row + offset, row))
    return out


def parse_csv(text: str) -> list[ImportItem]:
    text = text.removeprefix(BOM)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if r]
    if not rows:
        raise ImportError_("the file is empty")
    return _rows_to_items(_rows_from_table(list(rows[0]), rows[1:], 2))  # type: ignore[arg-type]


def parse_xlsx(data: bytes) -> list[ImportItem]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ImportError_(f"cannot read the workbook: {exc}") from exc
    try:
        sheet = wb.worksheets[0]
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    finally:
        wb.close()
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        raise ImportError_("the workbook is empty")
    return _rows_to_items(_rows_from_table(rows[0], rows[1:], 2))


def parse_pagewatch(text: str) -> tuple[ExportDocument, list[ImportItem]]:
    try:
        doc = ExportDocument.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise ImportError_(f"not a PageWatch export: {str(exc)[:200]}") from exc
    if doc.version > EXPORT_VERSION:
        raise ImportError_(
            f"this export is version {doc.version}; this build reads up to {EXPORT_VERSION}"
        )
    items = [
        ImportItem(n, b.url, b.name, [], None, None, b, b.folder_id)
        for n, b in enumerate(doc.bookmarks, 1)
    ]
    return doc, items


def detect_format(filename: str | None, text: str | None, data: bytes | None) -> ImportFormat:
    name = (filename or "").lower()
    if data is not None and data[:2] == b"PK":
        return "xlsx"
    for ext, fmt in ((".xlsx", "xlsx"), (".xlsm", "xlsx"), (".csv", "csv"), (".json", "json"),
                     (".html", "netscape"), (".htm", "netscape"), (".txt", "urls")):  # fmt: skip
        if name.endswith(ext):
            return fmt  # type: ignore[return-value]
    head = (text or "").removeprefix(BOM).lstrip()[:2000].lower()
    if head.startswith("{") and "pagewatch-export" in head:
        return "json"
    if "<dt>" in head or "<!doctype netscape-bookmark" in head or "<dl>" in head:
        return "netscape"
    first = head.splitlines()[0] if head else ""
    if "," in first and "url" in first:
        return "csv"
    return "urls"


# -- the importer -----------------------------------------------------------------------


@dataclass(slots=True)
class _Plan:
    created: int = 0
    skipped: int = 0
    folders: int = 0
    errors: list[ImportRowError] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)


class _Folders:
    """Folder paths -> ids, creating what is missing (or only counting it, in a dry run)."""

    def __init__(self, engine: Engine, rows: list[sqlite3.Row], dry_run: bool) -> None:
        self.engine = engine
        self.dry_run = dry_run
        self.by_key: dict[tuple[int | None, str], int] = {}
        for r in rows:
            if not r["is_virtual"]:
                self.by_key.setdefault((r["parent_id"], r["name"].lower()), r["id"])
        self.created = 0
        self._fake = -1

    async def ensure(self, parent: int | None, name: str) -> int:
        key = (parent, name.lower())
        if key in self.by_key:
            return self.by_key[key]
        self.created += 1
        if self.dry_run:
            self._fake -= 1
            self.by_key[key] = self._fake
            return self._fake
        data = {"parent_id": parent if parent is None or parent > 0 else None, "name": name[:200]}
        fid = await self.engine.db.write(lambda c: repo.folder_insert(c, data))
        await self.engine.reload_folders()
        self.by_key[key] = fid
        return fid

    async def path(self, base: int | None, parts: list[str]) -> int | None:
        parent = base
        for part in parts:
            parent = await self.ensure(parent, part)
        return parent


def _parse_any(
    req: ImportRequest,
) -> tuple[str, list[ImportItem], ExportDocument | None]:
    data: bytes | None = None
    text: str | None = req.content
    if req.content_base64 is not None:
        try:
            data = base64.b64decode(req.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImportError_("'content_base64' is not valid base64") from exc
        text = None
    fmt: str = req.format if req.format != "auto" else detect_format(req.filename, text, data)
    if fmt == "xlsx":
        if data is None:
            raise ImportError_("an xlsx file must be sent as 'content_base64'")
        return fmt, parse_xlsx(data), None
    if text is None:
        assert data is not None
        text = data.decode("utf-8-sig", errors="replace")
    if fmt == "netscape":
        return fmt, parse_netscape(text), None
    if fmt == "csv":
        return fmt, parse_csv(text), None
    if fmt == "json":
        doc, items = parse_pagewatch(text)
        return fmt, items, doc
    return "urls", parse_urls(text), None


async def run_import(engine: Engine, req: ImportRequest) -> ImportResult:
    try:
        fmt, items, doc = _parse_any(req)
    except ImportError_ as exc:
        raise svc.InvalidError(str(exc)) from exc
    if req.folder_id is not None:
        if not engine.folders.exists(req.folder_id):
            raise svc.InvalidError(f"folder {req.folder_id} does not exist")
        if engine.folders.is_virtual(req.folder_id):
            raise svc.InvalidError("bookmarks cannot be imported into a saved search")

    def load(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], list[str]]:
        urls = [r["url"] for r in conn.execute("SELECT url FROM bookmark")]
        return repo.folder_list(conn), urls

    folder_rows, existing = await engine.db.read(load)
    seen = {normalize_url(u) for u in existing}
    folders = _Folders(engine, folder_rows, req.dry_run)
    plan = _Plan()
    id_map: dict[int, int | None] = {}
    if doc is not None:
        id_map = await _import_folders(engine, doc, folders, req.folder_id)

    for item in items:
        try:
            if item.error:
                raise ValueError(item.error)
            key = normalize_url(item.url)
            if key in seen:
                plan.skipped += 1
                continue
            body = await _bookmark_in(engine, item, folders, req.folder_id, id_map)
        except (ValueError, ValidationError) as exc:
            plan.errors.append(ImportRowError(row=item.row, message=_message(exc)))
            continue
        seen.add(key)
        if req.dry_run:
            plan.created += 1
            continue
        try:
            row = await svc.create(engine, body)
        except (svc.InvalidError, svc.NotFoundError) as exc:
            plan.errors.append(ImportRowError(row=item.row, message=str(exc)))
            seen.discard(key)
            continue
        plan.created += 1
        plan.ids.append(row["id"])
    return ImportResult(
        format=fmt, dry_run=req.dry_run, created=plan.created, skipped_duplicates=plan.skipped,
        folders_created=folders.created, errors=plan.errors, bookmark_ids=plan.ids,
    )  # fmt: skip


def _message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return f"{loc}: {first['msg']}" if loc else str(first["msg"])
    return str(exc)


async def _bookmark_in(
    engine: Engine,
    item: ImportItem,
    folders: _Folders,
    base: int | None,
    id_map: dict[int, int | None],
) -> BookmarkIn:
    if item.full is not None:
        b = item.full
        target = id_map.get(b.folder_id) if b.folder_id is not None else base
        if b.folder_id is not None and b.folder_id not in id_map:
            target = base
        return BookmarkIn.model_validate(
            {
                "name": b.name, "url": b.url,
                "folder_id": target if target is None or target > 0 else None,
                "source_type": b.source_type.value, "check_method": b.check_method.value,
                "enabled": b.enabled, "priority": b.priority,
                "highlight_mode": b.highlight_mode.value,
                "schedule": b.schedule or None, "fetch": b.fetch or None,
                "filter": b.filter or None, "gate": b.gate or None,
                "actions": b.actions or None, "plugin": b.plugin,
                "info1": b.info1, "info2": b.info2, "info3": b.info3, "note": b.note,
            }
        )  # fmt: skip
    folder_id = await folders.path(base, item.folder) if item.folder else base
    gate = {"keywords": item.keywords} if item.keywords else None
    schedule = {"interval_s": item.interval_s} if item.interval_s else None
    return BookmarkIn.model_validate(
        {
            "name": item.name[:300] if item.name else None,
            "url": item.url,
            "folder_id": folder_id if folder_id is None or folder_id > 0 else None,
            "schedule": schedule,
            "gate": gate,
        }
    )


async def _import_folders(
    engine: Engine, doc: ExportDocument, folders: _Folders, base: int | None
) -> dict[int, int | None]:
    """Recreate an export's folder tree under ``base`` (matching same-named folders)."""
    by_id = {f.id: f for f in doc.folders}
    mapping: dict[int, int | None] = {}

    async def place(f: ExportFolder) -> int | None:
        if f.id in mapping:
            return mapping[f.id]
        parent = base if f.parent_id is None else await place(by_id[f.parent_id])
        if f.is_virtual:
            made = await _virtual_folder(engine, f, parent, folders.dry_run)
        else:
            key = (parent, f.name.lower())
            if key in folders.by_key:
                made = folders.by_key[key]
            else:
                made = await folders.ensure(parent, f.name)
                if f.defaults and not folders.dry_run:
                    await engine.db.write(
                        lambda c: repo.folder_update(c, made, {"defaults_json": dumps(f.defaults)})
                    )
                    await engine.reload_folders()
        mapping[f.id] = made
        return made

    for f in doc.folders:
        if f.parent_id is None or f.parent_id in by_id:
            await place(f)
    return mapping


async def _virtual_folder(
    engine: Engine, f: ExportFolder, parent: int | None, dry_run: bool
) -> int | None:
    if dry_run:
        return None
    data = {
        "parent_id": parent if parent and parent > 0 else None, "name": f.name,
        "is_virtual": True, "query_json": dumps(f.query or {}),
    }  # fmt: skip
    fid = await engine.db.write(lambda c: repo.folder_insert(c, data))
    await engine.reload_folders()
    return fid


# -- export -----------------------------------------------------------------------------


def folder_names(rows: list[sqlite3.Row]) -> dict[int, str]:
    by_id = {r["id"]: r for r in rows}
    out: dict[int, str] = {}
    for fid in by_id:
        parts: list[str] = []
        cur: int | None = fid
        guard = 0
        while cur is not None and cur in by_id and guard < 64:
            parts.append(by_id[cur]["name"])
            cur = by_id[cur]["parent_id"]
            guard += 1
        out[fid] = "/".join(reversed(parts))
    return out


def _bookmark_rows(
    conn: sqlite3.Connection, folder_id: int | None, ids: list[int] | None
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    folders = repo.folder_list(conn)
    where, args = "", []
    if ids:
        where = f" WHERE id IN ({','.join('?' * len(ids))})"
        args = list(ids)
    elif folder_id is not None:
        tree = repo.folder_with_descendants(conn, folder_id)
        where = f" WHERE folder_id IN ({','.join('?' * len(tree))})"
        args = tree
    rows = conn.execute(f"SELECT * FROM bookmark{where} ORDER BY id", args).fetchall()
    return folders, rows


def _is_feed(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if row["source_type"] == "feed":
        return True
    v = repo.version_get(conn, row["latest_version_id"])
    ctype = ((v["content_type"] if v else "") or "").lower()
    return any(t in ctype for t in _FEED_TYPES)


async def export_json(
    engine: Engine, *, folder_id: int | None = None, ids: list[int] | None = None
) -> ExportDocument:
    folders, rows = await engine.db.read(lambda c: _bookmark_rows(c, folder_id, ids))
    keep = {r["id"] for r in folders}
    if folder_id is not None and not ids:
        keep = set(await engine.db.read(lambda c: repo.folder_with_descendants(c, folder_id)))
    elif ids:
        used = {r["folder_id"] for r in rows if r["folder_id"] is not None}
        keep = set(used)
        for fid in list(used):  # their ancestors, so the tree is whole
            cur = next((f["parent_id"] for f in folders if f["id"] == fid), None)
            while cur is not None and cur not in keep:
                keep.add(cur)
                cur = next((f["parent_id"] for f in folders if f["id"] == cur), None)
    out_folders = [
        ExportFolder(
            id=f["id"], parent_id=f["parent_id"] if f["parent_id"] in keep else None,
            name=f["name"], sort_order=f["sort_order"], is_virtual=bool(f["is_virtual"]),
            query=loads(f["query_json"], None), defaults=loads(f["defaults_json"], {}) or {},
        )
        for f in folders
        if f["id"] in keep and (not f["is_virtual"] or ids is None)
    ]  # fmt: skip
    marks = {f.id for f in out_folders}
    bookmarks = [_export_bookmark(r, marks) for r in rows]
    return ExportDocument(
        exported_at=iso(engine.clock.now()), app_version=__version__,
        folders=out_folders, bookmarks=bookmarks,
    )  # fmt: skip


def _export_bookmark(r: sqlite3.Row, folder_ids: set[int]) -> ExportBookmark:
    actions = loads(r["actions_json"], {})
    return ExportBookmark(
        name=r["name"], url=r["url"],
        folder_id=r["folder_id"] if r["folder_id"] in folder_ids else None,
        source_type=r["source_type"], check_method=r["check_method"], enabled=bool(r["enabled"]),
        priority=r["priority"], highlight_mode=r["highlight_mode"],
        schedule=loads(r["schedule_json"], {}) or {}, fetch=loads(r["fetch_json"], {}) or {},
        filter=loads(r["filter_json"], {}) or {}, gate=loads(r["gate_json"], {}) or {},
        actions=actions if actions else {}, plugin=r["plugin"],
        info1=r["info1"], info2=r["info2"], info3=r["info3"], note=r["note"],
    )  # fmt: skip


async def export_opml(
    engine: Engine, *, folder_id: int | None = None, ids: list[int] | None = None
) -> str:
    def read(conn: sqlite3.Connection) -> tuple[dict[int, str], list[sqlite3.Row]]:
        folders, rows = _bookmark_rows(conn, folder_id, ids)
        return folder_names(folders), [r for r in rows if _is_feed(conn, r)]

    names, feeds = await engine.db.read(read)
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in feeds:
        groups.setdefault(names.get(r["folder_id"], "") if r["folder_id"] else "", []).append(r)

    def outline(r: sqlite3.Row, indent: str) -> str:
        return (
            f'{indent}<outline type="rss" text={quoteattr(r["name"])} '
            f"title={quoteattr(r['name'])} xmlUrl={quoteattr(r['url'])}/>"
        )

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">', "  <head>",
             "    <title>PageWatch feeds</title>", f"    <dateCreated>{iso(engine.clock.now())}"
             "</dateCreated>", "  </head>", "  <body>"]  # fmt: skip
    for group in sorted(groups):
        rows = groups[group]
        if not group:
            lines += [outline(r, "    ") for r in rows]
            continue
        lines.append(f"    <outline text={quoteattr(group)}>")
        lines += [outline(r, "      ") for r in rows]
        lines.append("    </outline>")
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)


def fmt_interval(seconds: int | None) -> str:
    if not seconds:
        return ""
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


async def export_csv(
    engine: Engine, *, folder_id: int | None = None, ids: list[int] | None = None
) -> str:
    def read(conn: sqlite3.Connection) -> tuple[dict[int, str], list[sqlite3.Row]]:
        folders, rows = _bookmark_rows(conn, folder_id, ids)
        return folder_names(folders), rows

    names, rows = await engine.db.read(read)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(
        ["id", "name", "url", "folder", "interval", "keywords", "enabled", "status",
         "source_type", "check_method", "last_checked", "last_changed", "errors"]
    )  # fmt: skip
    for r in rows:
        try:
            resolved = resolve(r, engine.folders, engine.settings)
            interval = fmt_interval(resolved.schedule.interval_s)
            keywords = resolved.gate.keywords
        except ValidationError:
            interval, keywords = "", ""
        writer.writerow(
            [r["id"], r["name"], r["url"], names.get(r["folder_id"], "") if r["folder_id"] else "",
             interval, keywords, "yes" if r["enabled"] else "no", r["status"], r["source_type"],
             r["check_method"], r["last_checked_at"] or "", r["last_changed_at"] or "",
             r["consecutive_errors"]]
        )  # fmt: skip
    return BOM + out.getvalue()  # a BOM, so Excel reads the UTF-8 correctly


async def export_text(
    engine: Engine, fmt: str, *, folder_id: int | None = None, ids: list[int] | None = None
) -> tuple[str, str, str]:
    """``(body, media type, file extension)`` for ``GET /export``."""
    if fmt == "json":
        doc = await export_json(engine, folder_id=folder_id, ids=ids)
        return doc.model_dump_json(indent=2), "application/json", "json"
    if fmt == "opml":
        return await export_opml(engine, folder_id=folder_id, ids=ids), "text/x-opml", "opml"
    if fmt == "csv":
        return await export_csv(engine, folder_id=folder_id, ids=ids), "text/csv", "csv"
    raise svc.InvalidError(f"unknown export format {fmt!r}; use json, opml or csv")


__all__ = [
    "ImportError_", "ImportItem", "detect_format", "export_csv", "export_json", "export_opml",
    "export_text", "fmt_interval", "folder_names", "folder_path", "parse_csv", "parse_interval",
    "parse_netscape", "parse_pagewatch", "parse_urls", "parse_xlsx", "run_import",
]  # fmt: skip
