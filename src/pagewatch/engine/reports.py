"""Reports (spec: Import, export, reports and backup): Jinja2 templates rendered to HTML, CSV, JSON
or XML, on demand, from the CLI, or as an action.

Three reports ship with the engine: ``changed`` (changes since the last report), ``all``
(every bookmark with its status) and ``errors``. Your own templates live in ``<data>/reports/``
as ``<name>.<format>.j2`` (``weekly.html.j2``) and see the same data. Templates run in Jinja2's
sandbox. The "last report" marker is kept per template; asking for a report moves it (to the newest
change it listed) unless the caller says otherwise. Changes are matched strictly after the marker.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import kv
from pagewatch.engine.clock import iso
from pagewatch.engine.config import resolve
from pagewatch.engine.portability import fmt_interval, folder_names
from pagewatch.engine.store import repo
from pagewatch.models import ReportInfo, loads

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

FORMATS = ("html", "csv", "json", "xml")
MEDIA = {
    "html": "text/html",
    "csv": "text/csv",
    "json": "application/json",
    "xml": "application/xml",
}
DEFAULT_WINDOW = timedelta(hours=24)  # "changed" with no earlier report looks back this far
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,60}$")

STATUS_COLUMNS = [
    ("id", "ID"), ("name", "Name"), ("url", "URL"), ("folder", "Folder"), ("status", "Status"),
    ("unread", "Unread"), ("interval", "Every"), ("last_checked", "Last checked"),
    ("last_changed", "Last changed"), ("errors", "Errors"),
]  # fmt: skip
CHANGE_COLUMNS = [
    ("detected_at", "Detected"), ("name", "Bookmark"), ("url", "URL"), ("summary", "Change"),
    ("added_words", "Added words"), ("removed_words", "Removed words"),
    ("checks", "Checks"), ("keywords", "Keywords"),
]  # fmt: skip
ERROR_COLUMNS = [*STATUS_COLUMNS[:5], ("reason", "Last error"), ("errors", "Errors"),
                 ("last_checked", "Last checked")]  # fmt: skip


@dataclass(frozen=True, slots=True)
class BuiltIn:
    title: str
    columns: list[tuple[str, str]]


BUILTINS = {
    "changed": BuiltIn("Changed since the last report", CHANGE_COLUMNS),
    "all": BuiltIn("All bookmarks", STATUS_COLUMNS),
    "errors": BuiltIn("Bookmarks with errors", ERROR_COLUMNS),
}


class ReportError(ValueError):
    pass


# -- the templates ----------------------------------------------------------------------


def _builtin_template(fmt: str) -> str:
    return (
        resources.files("pagewatch.data")
        .joinpath(f"reports/table.{fmt}.j2")
        .read_text(encoding="utf-8")
    )


def _env(fmt: str) -> SandboxedEnvironment:
    # Escape for the markup formats only. (``select_autoescape`` would escape every template
    # rendered from a string, which turns "&" into "&amp;" in a CSV or JSON report.)
    return SandboxedEnvironment(autoescape=fmt in ("html", "xml"), keep_trailing_newline=True)


def user_templates(reports_dir: Path) -> dict[str, dict[str, Path]]:
    """``{name: {format: file}}`` for ``<name>.<format>.j2`` files in the reports folder."""
    found: dict[str, dict[str, Path]] = {}
    if reports_dir.is_dir():
        for path in sorted(reports_dir.glob("*.j2")):
            name, _, fmt = path.name[: -len(".j2")].rpartition(".")
            if name and fmt in FORMATS and _NAME.match(name):
                found.setdefault(name, {})[fmt] = path
    return found


def reports_dir(engine: Engine) -> Path:
    return engine.data_dir.root / "reports"


def list_reports(engine: Engine) -> list[ReportInfo]:
    out = [
        ReportInfo(name=n, title=b.title, builtin=True, formats=list(FORMATS))
        for n, b in BUILTINS.items()
    ]
    for name, files in user_templates(reports_dir(engine)).items():
        if name not in BUILTINS:
            out.append(ReportInfo(name=name, title=name, builtin=False, formats=sorted(files)))
    return out


# -- the data ---------------------------------------------------------------------------


def _row(
    engine: Engine, r: sqlite3.Row, names: dict[int, str], hits: dict[int, list[str]]
) -> dict[str, Any]:
    try:
        interval = fmt_interval(resolve(r, engine.folders, engine.settings).schedule.interval_s)
    except Exception:
        interval = ""
    return {
        "id": r["id"], "name": r["name"], "url": r["url"],
        "folder": names.get(r["folder_id"], "") if r["folder_id"] else "",
        "status": r["status"], "unread": "yes" if r["unread"] else "",
        "enabled": bool(r["enabled"]),
        "interval": interval, "last_checked": r["last_checked_at"] or "",
        "last_changed": r["last_changed_at"] or "", "errors": r["consecutive_errors"],
        "keywords": ", ".join(hits.get(r["id"], [])), "reason": "",
    }  # fmt: skip


async def _since(engine: Engine, name: str, requested: str | None) -> str:
    if requested:
        return requested
    marker = await engine.db.read(lambda c: kv.get(c, kv.REPORT_MARKER, name))
    return str(marker) if marker else iso(engine.clock.now() - DEFAULT_WINDOW)


async def build_data(
    engine: Engine, name: str, *, since: str | None = None, folder_id: int | None = None
) -> dict[str, Any]:
    """The values every template sees."""
    base = BUILTINS.get(name)
    columns = base.columns if base else STATUS_COLUMNS
    title = base.title if base else name
    wants_changes = name not in ("all", "errors")
    since_iso = await _since(engine, name, since) if wants_changes else None
    now = iso(engine.clock.now())

    def read(conn: sqlite3.Connection) -> dict[str, Any]:
        folders = repo.folder_list(conn)
        names = folder_names(folders)
        tree = repo.folder_with_descendants(conn, folder_id) if folder_id is not None else None
        scope = "" if tree is None else f" AND folder_id IN ({','.join('?' * len(tree))})"
        args: list[Any] = list(tree or [])
        rows = conn.execute(f"SELECT * FROM bookmark WHERE 1=1{scope} ORDER BY id", args).fetchall()
        hits = repo.unread_keyword_hits(conn, [r["id"] for r in rows])
        items = [_row(engine, r, names, hits) for r in rows]
        by_id = {r["id"]: r for r in rows}
        changes: list[dict[str, Any]] = []
        if since_iso is not None:
            for c in conn.execute(
                "SELECT * FROM change WHERE detected_at > ? ORDER BY detected_at, id",
                (since_iso,),
            ):
                b = by_id.get(c["bookmark_id"])
                if b is None:
                    continue
                kws = loads(c["keyword_hits_json"], []) or []
                changes.append(
                    {
                        "id": c["id"], "bookmark_id": b["id"], "name": b["name"], "url": b["url"],
                        "detected_at": c["detected_at"], "summary": c["summary"] or "",
                        "added_words": c["added_words"] or 0,
                        "removed_words": c["removed_words"] or 0,
                        "checks": c["checks_accumulated"], "keywords": ", ".join(kws),
                    }
                )  # fmt: skip
        if name == "errors":
            failing = [
                (r, i) for r, i in zip(rows, items, strict=True)
                if r["status"] in ("error", "needs_login") or r["consecutive_errors"] > 0
            ]  # fmt: skip
            for r, item in failing:
                last = conn.execute(
                    "SELECT reason FROM check_run WHERE bookmark_id=? AND outcome='error' "
                    "ORDER BY id DESC LIMIT 1",
                    (r["id"],),
                ).fetchone()
                item["reason"] = (last["reason"] if last else "") or r["status"]
            items = [i for _, i in failing]
        if name == "changed":
            wanted = {c["bookmark_id"] for c in changes}
            listed = [i for i in items if i["id"] in wanted]
            rows_out: list[dict[str, Any]] = changes
        else:
            listed, rows_out = items, items
        return {"bookmarks": listed, "changes": changes, "rows": rows_out}

    data = await engine.db.read(read)
    return {
        "name": name, "title": title, "generated_at": now, "since": since_iso,
        "columns": columns, "folder_id": folder_id,
        "counts": {
            "rows": len(data["rows"]), "bookmarks": len(data["bookmarks"]),
            "changes": len(data["changes"]),
        },
        **data,
    }  # fmt: skip


# -- rendering --------------------------------------------------------------------------


def _csv(data: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([header for _, header in data["columns"]])
    for row in data["rows"]:
        writer.writerow([row.get(key, "") for key, _ in data["columns"]])
    return out.getvalue()


def _json(data: dict[str, Any]) -> str:
    keep = {k: data[k] for k in ("name", "title", "generated_at", "since", "counts", "rows")}
    keep["columns"] = [{"key": k, "header": h} for k, h in data["columns"]]
    return json.dumps(keep, indent=2, ensure_ascii=False)


def render(engine: Engine, name: str, fmt: str, data: dict[str, Any]) -> str:
    """The report as text. Raises ``ReportError`` for an unknown template or format."""
    if fmt not in FORMATS:
        raise ReportError(f"unknown report format {fmt!r}; use one of {', '.join(FORMATS)}")
    custom = user_templates(reports_dir(engine)).get(name, {})
    try:
        if fmt in custom:
            source = custom[fmt].read_text(encoding="utf-8")
            return _env(fmt).from_string(source).render(**data)
        if name not in BUILTINS:
            have = ", ".join(sorted(custom)) or "none"
            raise ReportError(f"report {name!r} has no {fmt} template (it has: {have})")
        if fmt == "csv":
            return _csv(data)
        if fmt == "json":
            return _json(data)
        return _env(fmt).from_string(_builtin_template(fmt)).render(**data)
    except TemplateError as exc:
        raise ReportError(f"template {name}.{fmt}.j2: {exc}") from exc
    except OSError as exc:
        raise ReportError(f"cannot read template {name}.{fmt}.j2: {exc}") from exc


def known(engine: Engine, name: str) -> bool:
    return name in BUILTINS or name in user_templates(reports_dir(engine))


def default_format(engine: Engine, name: str) -> str:
    """HTML when the report has it (every built-in does), else its first format in the
    order html, csv, json, xml."""
    have = user_templates(reports_dir(engine)).get(name, {})
    return next((f for f in FORMATS if f in have or name in BUILTINS), "html")


async def generate(
    engine: Engine,
    name: str,
    *,
    fmt: str | None = None,
    since: str | None = None,
    mark: bool = True,
    folder_id: int | None = None,
) -> tuple[str, str]:
    """``(text, format)``; moves the template's "last report" marker to now when ``mark``."""
    if not known(engine, name):
        raise svc.NotFoundError(f"no report named {name!r}")
    chosen = fmt or default_format(engine, name)
    data = await build_data(engine, name, since=since, folder_id=folder_id)
    text = await asyncio.to_thread(render, engine, name, chosen, data)
    if mark:
        # The newest change in this report, not "now": the next report starts strictly after it,
        # so a change is listed once, never twice and never missed.
        stamp = max((c["detected_at"] for c in data["changes"]), default=data["generated_at"])
        await engine.db.write(lambda c: kv.put(c, kv.REPORT_MARKER, name, stamp))
    return text, chosen
