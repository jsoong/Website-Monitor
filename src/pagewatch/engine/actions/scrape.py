"""The ``scrape`` action: append one record per change to a CSV, JSON Lines or XML file.

Each field is read from the *new* version of the page: a CSS selector or XPath (the element's
text, or an attribute), or a regular expression over the page's text (group 1 if there is one).
Every record also carries ``change_id``, ``detected_at``, ``bookmark_id``, ``bookmark`` and
``url``. Reading the page is parse work, so it runs in the worker pool.

* **Idempotent per change.** A job that runs twice after a crash must not write the change
  twice: the file is checked for this ``change_id`` (the last 256 KB for CSV and JSON Lines,
  the whole document for XML) before appending.
* **Columns are fixed by the first record.** A file whose header differs from the configured
  fields is a configuration problem, not something to guess about: the job fails with a message
  saying so, rather than writing misaligned columns.
* **CSV is written as UTF-8 with a byte-order mark** (Excel opens it correctly) and values that
  a spreadsheet would run as a formula (``=``, ``@``, ``+``, ``-`` followed by non-numbers) get a
  leading apostrophe, because the text came from a web page.
* Appends from several bookmarks to one file are serialised.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lxml import etree
from lxml.cssselect import CSSSelector

from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import parse_params, write_atomic
from pagewatch.engine.actions.content import load_change_rows
from pagewatch.engine.actions.export import resolve_path
from pagewatch.engine.config import resolve, source_options
from pagewatch.engine.logs import get_logger
from pagewatch.engine.pipeline import sources
from pagewatch.engine.pipeline.extract import blocks_from_json, decode_body, parse_html
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.models import SCRAPE_RESERVED, ScrapeParams

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.scrape")

TAIL_BYTES = 256 * 1024
MAX_VALUE = 2000
MAX_XML_BYTES = 50 * 1024 * 1024
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_PLAIN_NUMBER = re.compile(r"^[+-]?[\d.,]*\d[\d.,]*$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


# -- reading the fields (worker) ------------------------------------------------------------


@dataclass(slots=True)
class ScrapeJob:
    blob_root: str
    url: str
    new_blocks_hash: str
    new_raw_hash: str | None
    content_type: str
    source_type: str
    source_cfg: dict[str, Any]
    fields: list[dict[str, Any]]


def _text_of(el: Any, attr: str | None) -> str:
    if attr:
        return str(el.get(attr) or "")
    return " ".join("".join(el.itertext()).split())


def _from_dom(field: dict[str, Any], root: Any) -> list[str]:
    if root is None:
        return []
    if field["kind"] == "css":
        found: Any = CSSSelector(field["expr"])(root)
    else:
        found = root.xpath(field["expr"])
    if not isinstance(found, list):
        found = [found]
    out: list[str] = []
    for item in found:
        if isinstance(item, etree._Element):
            out.append(_text_of(item, field.get("attr")))
        elif isinstance(item, bool | int | float):
            out.append(str(item))
        else:  # an attribute or text() result
            out.append(" ".join(str(item).split()))
    return [t for t in out if t]


def _from_text(field: dict[str, Any], text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(field["expr"], text, re.MULTILINE):
        value = m.group(1) if m.re.groups else m.group(0)
        if value:
            out.append(" ".join(str(value).split()))
    return out


def scrape_fields(job: ScrapeJob) -> dict[str, str]:
    """Worker entry point: read every configured field from the new version."""
    store = BlobStore(Path(job.blob_root))
    root = None
    if job.new_raw_hash:
        raw, ctype = sources.view_bytes(
            store.get(job.new_raw_hash), job.content_type, job.url, job.source_type, job.source_cfg
        )  # fmt: skip
        root = parse_html(decode_body(raw, ctype))
    text = "\n".join(b.text for b in blocks_from_json(store.get_json(job.new_blocks_hash)))
    out: dict[str, str] = {}
    for field in job.fields:
        values = _from_text(field, text) if field["kind"] == "regex" else _from_dom(field, root)
        if not values:
            out[field["name"]] = ""
        elif field.get("multiple"):
            out[field["name"]] = " | ".join(values)[:MAX_VALUE]
        else:
            out[field["name"]] = values[0][:MAX_VALUE]
    return out


# -- appending (thread) ---------------------------------------------------------------------


def _clean(value: object) -> str:
    return _XML_BAD.sub("", "" if value is None else str(value))


def _spreadsheet_safe(value: str) -> str:
    if value[:1] in ("=", "@", "\t", "\r") or (
        value[:1] in ("+", "-") and not _PLAIN_NUMBER.match(value)
    ):
        return "'" + value
    return value


def _tail(path: Path) -> str:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if size > TAIL_BYTES:
        text = text.partition("\n")[2]  # the first line is probably cut in half
    return text


def _append_csv(path: Path, columns: list[str], record: dict[str, str], change_id: int) -> bool:
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), None)
        if header != columns:
            raise ActionConfigError(
                f"scrape: {path.name} has the columns {header}, but the action is set to write "
                f"{columns}; change the fields back or choose another file"
            )
        for row in csv.reader(io.StringIO(_tail(path))):
            if row and row[0] == str(change_id):
                return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(
        path, "w" if new else "a", encoding="utf-8-sig" if new else "utf-8", newline=""
    ) as fh:
        writer = csv.writer(fh, lineterminator="\n")
        if new:
            writer.writerow(columns)
        writer.writerow([_spreadsheet_safe(record[c]) for c in columns])
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _append_jsonl(path: Path, columns: list[str], record: dict[str, str], change_id: int) -> bool:
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        for line in _tail(path).splitlines():
            try:
                if json.loads(line).get("change_id") == change_id:
                    return False
            except (ValueError, AttributeError):
                continue
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if exists:
        with open(path, "rb") as check:
            check.seek(-1, 2)
            needs_newline = check.read(1) != b"\n"  # an earlier write was cut short
    obj: dict[str, object] = {c: record[c] for c in columns}
    obj["change_id"] = change_id
    obj["bookmark_id"] = int(record["bookmark_id"])
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "ab") as fh:
        if needs_newline:
            fh.write(b"\n")
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _append_xml(path: Path, columns: list[str], record: dict[str, str], change_id: int) -> bool:
    if path.exists() and path.stat().st_size > 0:
        if path.stat().st_size > MAX_XML_BYTES:
            raise ActionConfigError(f"scrape: {path.name} is too large to append to as XML")
        try:
            root = etree.parse(str(path)).getroot()
        except etree.XMLSyntaxError as exc:
            raise ActionConfigError(f"scrape: {path.name} is not valid XML: {exc}") from exc
        if root.xpath("record[@change_id=$c]", c=str(change_id)):
            return False
    else:
        root = etree.Element("records")
    rec = etree.SubElement(root, "record", change_id=str(change_id))
    for c in columns:
        if c != "change_id":
            etree.SubElement(rec, c).text = record[c]
    write_atomic(
        path, etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)
    )
    return True


APPENDERS = {"csv": _append_csv, "jsonl": _append_jsonl, "xml": _append_xml}


def append_record(
    path: Path, fmt: str, columns: list[str], record: dict[str, str], change_id: int
) -> bool:
    """Append one record. ``False`` if this change is already in the file."""
    with _path_lock(path):
        return APPENDERS[fmt](path, columns, record, change_id)


# -- the action -----------------------------------------------------------------------------


async def scrape(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(ScrapeParams, ctx.params, "scrape")
    target = resolve_path(engine, ctx, params.path, "scrape")

    change, row, _old, new = await engine.db.read(lambda c: load_change_rows(c, ctx.change_id))
    if change is None or row is None or new is None:
        raise ActionConfigError("scrape: the change no longer exists")
    resolved = resolve(row, engine.folders, engine.settings)
    values = await engine.pool.run(
        scrape_fields,
        ScrapeJob(
            blob_root=str(engine.data_dir.blobs_dir),
            url=row["url"],
            new_blocks_hash=new["blocks_hash"],
            new_raw_hash=new["raw_hash"],
            content_type=new["content_type"] or "",
            source_type=row["source_type"],
            source_cfg=source_options(resolved.fetch),
            fields=[f.model_dump() for f in params.fields],
        ),
    )
    columns = [*SCRAPE_RESERVED, *(f.name for f in params.fields)]
    record = {
        "change_id": str(ctx.change_id),
        "detected_at": ctx.detected_at,
        "bookmark_id": str(ctx.bookmark_id),
        "bookmark": ctx.name,
        "url": ctx.url,
        **values,
    }
    record = {k: _clean(v) for k, v in record.items()}
    written = await asyncio.to_thread(
        append_record, target, params.format, columns, record, ctx.change_id
    )
    log.info(
        "scrape_record", bookmark=ctx.bookmark_id, change=ctx.change_id, path=str(target),
        written=written,
    )  # fmt: skip
