"""CLI commands of the power features (M7): ``report``, ``import``, ``export``, ``plugin``, and the
extra options of ``add`` and ``folder add`` (plugin, Follow-Links, merge pages, saved searches).

Kept apart from ``main.py`` so that module only gains a few hook lines. ``main.py`` calls
``register`` while building the parser, and ``run`` / ``render`` for the commands named in
``COMMANDS``.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

from pagewatch.cli.client import ApiError, EngineClient

COMMANDS = frozenset({"report", "import", "export", "plugin"})
EXT_FORMATS = {".html": "html", ".htm": "html", ".csv": "csv", ".json": "json", ".xml": "xml"}
EXPORT_EXTS = {".json": "json", ".opml": "opml", ".xml": "opml", ".csv": "csv"}


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return value


def register(sub: Any) -> None:
    rep = sub.add_parser("report", help="generate a report (HTML, CSV, JSON or XML)")
    rep.add_argument("template", nargs="?", help="changed, all, errors, or one of your own")
    rep.add_argument("--list", action="store_true", help="list the available reports")
    rep.add_argument("--out", "-o", help="write to this file (default: print it)")
    rep.add_argument("--format", choices=["html", "csv", "json", "xml"],
                     help="default: from --out's extension, else the report's own")  # fmt: skip
    rep.add_argument("--since", help="'changed' only: changes since this UTC time (ISO-8601)")
    rep.add_argument("--folder", help="only bookmarks in this folder (name or id)")
    rep.add_argument("--no-mark", action="store_true",
                     help="do not move the 'since the last report' marker")  # fmt: skip

    imp = sub.add_parser("import", help="import bookmarks (HTML, URL list, CSV, XLSX, JSON)")
    imp.add_argument("file")
    imp.add_argument("--format", choices=["auto", "netscape", "urls", "csv", "xlsx", "json"],
                     default="auto")  # fmt: skip
    imp.add_argument("--folder", help="import under this folder (name or id)")
    imp.add_argument("--dry-run", action="store_true", help="only report what would happen")

    exp = sub.add_parser("export", help="export bookmarks (JSON, OPML, CSV)")
    exp.add_argument("file")
    exp.add_argument("--format", choices=["json", "opml", "csv"],
                     help="default: from the file's extension, else json")  # fmt: skip
    exp.add_argument("--folder", help="only this folder (name or id)")
    exp.add_argument("ids", nargs="*", type=int, help="only these bookmarks")

    plug = sub.add_parser("plugin", help="list plugins, or install a shipped example")
    psub = plug.add_subparsers(dest="plugin_cmd", required=True)
    psub.add_parser("list")
    pin = psub.add_parser("install")
    pin.add_argument("name")
    pin.add_argument("--overwrite", action="store_true")


def add_options(add: argparse.ArgumentParser) -> None:
    """More options for ``add``."""
    add.add_argument("--plugin", help="a plugin from the plugins folder (see 'plugin list')")
    add.add_argument("--follow", action="store_true",
                     help="Follow-Links: watch each link of the page as a bookmark")  # fmt: skip
    add.add_argument("--follow-include", action="append", default=[], metavar="PATTERN",
                     help="only links matching this (wildcards * ?; repeatable)")  # fmt: skip
    add.add_argument("--follow-exclude", action="append", default=[], metavar="PATTERN")
    add.add_argument("--follow-regex", action="store_true", help="patterns are regular expressions")
    add.add_argument("--follow-depth", type=int, default=1)
    add.add_argument("--follow-pages", type=int, default=50, help="at most this many bookmarks")
    add.add_argument("--follow-any-host", action="store_true", help="also links to other sites")
    add.add_argument("--merge-next", metavar="SELECTOR",
                     help="merge pages: follow the next-page link (CSS selector)")  # fmt: skip
    add.add_argument("--merge-pattern", metavar="URL",
                     help="merge pages: numbered pages, e.g. https://x/list?page={n}")  # fmt: skip
    add.add_argument("--merge-pages", type=int, default=5, help="pages in all (default 5)")


def apply_add_options(args: argparse.Namespace, body: dict[str, Any]) -> None:
    if args.plugin:
        body["plugin"] = args.plugin
    fetch = dict(body.get("fetch") or {})
    if args.follow:
        fetch["follow"] = {
            "include": args.follow_include,
            "exclude": args.follow_exclude,
            "pattern_kind": "regex" if args.follow_regex else "wildcard",
            "depth": args.follow_depth,
            "max_pages": args.follow_pages,
            "same_host": not args.follow_any_host,
        }
    elif args.follow_include or args.follow_exclude:
        raise ApiError(2, "--follow-include / --follow-exclude need --follow")
    if args.merge_next and args.merge_pattern:
        raise ApiError(2, "give --merge-next or --merge-pattern, not both")
    if args.merge_next or args.merge_pattern:
        fetch["merge"] = {
            "next_selector": args.merge_next,
            "url_pattern": args.merge_pattern,
            "max_pages": args.merge_pages,
        }
    if fetch:
        body["fetch"] = fetch


def folder_options(fadd: argparse.ArgumentParser) -> None:
    fadd.add_argument("--virtual", action="store_true", help="a saved search instead of a folder")
    fadd.add_argument("--query", type=_json_object, metavar="JSON",
                      help='criteria, e.g. \'{"status": "error"}\'')  # fmt: skip


def folder_body(args: argparse.Namespace, body: dict[str, Any]) -> dict[str, Any]:
    if args.virtual:
        if args.query is None:
            raise ApiError(2, "a saved search needs --query")
        body = {**body, "is_virtual": True, "query": args.query}
    elif args.query is not None:
        raise ApiError(2, "--query only goes with --virtual")
    return body


# -- running ----------------------------------------------------------------------------


def _folder(client: EngineClient, ref: str | None) -> int | None:
    from pagewatch.cli.main import _folder_id

    return _folder_id(client, ref)


def run(args: argparse.Namespace, client: EngineClient) -> Any:
    cmd = args.cmd
    if cmd == "report":
        return _report(args, client)
    if cmd == "import":
        return _import(args, client)
    if cmd == "export":
        return _export(args, client)
    if cmd == "plugin":
        if args.plugin_cmd == "list":
            return client.get("/plugins")
        return client.post("/plugins/install", {"name": args.name, "overwrite": args.overwrite})
    raise ApiError(2, f"unknown command {cmd}")


def _report(args: argparse.Namespace, client: EngineClient) -> Any:
    if args.list or not args.template:
        return {"reports": client.get("/reports")}
    fmt = args.format
    if fmt is None and args.out:
        fmt = EXT_FORMATS.get(Path(args.out).suffix.lower())
    body: dict[str, Any] = {"mark": not args.no_mark}
    if fmt:
        body["format"] = fmt
    if args.since:
        body["since"] = args.since
    if args.folder:
        body["folder_id"] = _folder(client, args.folder)
    resp = client.raw("POST", f"/reports/{args.template}", json=body)
    if args.out:
        Path(args.out).write_bytes(resp.content)
        return {"written": args.out, "bytes": len(resp.content)}
    return {"text": resp.text}


def _import(args: argparse.Namespace, client: EngineClient) -> Any:
    path = Path(args.file)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ApiError(2, f"cannot read {args.file}: {exc.strerror or exc}") from exc
    body: dict[str, Any] = {"format": args.format, "filename": path.name, "dry_run": args.dry_run}
    if args.folder:
        body["folder_id"] = _folder(client, args.folder)
    if data[:2] == b"PK":
        body["content_base64"] = base64.b64encode(data).decode("ascii")
    else:
        body["content"] = data.decode("utf-8-sig", errors="replace")
    return client.post("/import", body)


def _export(args: argparse.Namespace, client: EngineClient) -> Any:
    fmt = args.format or EXPORT_EXTS.get(Path(args.file).suffix.lower(), "json")
    params: dict[str, Any] = {"format": fmt}
    if args.folder:
        params["folder"] = _folder(client, args.folder)
    if args.ids:
        params["ids"] = args.ids
    resp = client.raw("GET", "/export", params=params)
    Path(args.file).write_bytes(resp.content)
    return {"written": args.file, "format": fmt, "bytes": len(resp.content)}


# -- output -----------------------------------------------------------------------------


def render(args: argparse.Namespace, result: Any) -> str:
    cmd = args.cmd
    if cmd == "report":
        if "reports" in result:
            return "\n".join(
                f"{r['name']:<14} {'built in' if r['builtin'] else 'yours':<9} "
                f"{', '.join(r['formats'])}   {r['title']}"
                for r in result["reports"]
            )
        if "written" in result:
            return f"wrote {result['bytes']:,} bytes to {result['written']}"
        return str(result["text"]).rstrip("\n")
    if cmd == "import":
        head = "would import" if result["dry_run"] else "imported"
        lines = [
            f"{head} {result['created']} bookmark(s) from {result['format']}; "
            f"{result['skipped_duplicates']} already existed; "
            f"{result['folders_created']} folder(s) "
            f"{'would be ' if result['dry_run'] else ''}created"
        ]
        lines += [f"  row {e['row']}: {e['message']}" for e in result["errors"]]
        return "\n".join(lines)
    if cmd == "export":
        return f"wrote {result['bytes']:,} bytes ({result['format']}) to {result['written']}"
    if cmd == "plugin":
        if args.plugin_cmd == "install":
            return f"installed {result['name']} (hooks: {', '.join(result['hooks']) or 'none'})"
        rows = [
            f"{p['name']:<18} {'installed' if p['installed'] else 'available':<10} "
            f"{', '.join(p['hooks']) or '-':<26} {p['description'] or ''}"
            + (f"  [ERROR: {p['error']}]" if p["error"] else "")
            for p in result
        ]
        return "\n".join(rows) or "(no plugins)"
    return json.dumps(result)
