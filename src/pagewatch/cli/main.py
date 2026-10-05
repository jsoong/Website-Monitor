"""``pagewatch-cli``: scripting front end for the running engine."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pagewatch import __version__
from pagewatch.cli.client import ApiError, EngineClient, EngineNotRunning
from pagewatch.engine.paths import resolve_data_dir

_DURATION = re.compile(r"^(\d+)\s*([smhdw]?)$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """``90``, ``90s``, ``15m``, ``2h``, ``1d``, ``1w`` -> seconds."""
    m = _DURATION.match(text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (examples: 90s, 15m, 2h, 1d)")
    return int(m.group(1)) * _UNITS[m.group(2).lower()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pagewatch-cli", description="Control a running PageWatch engine"
    )
    p.add_argument("--data-dir", help="data folder of the engine to talk to")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=f"pagewatch-cli {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="add a bookmark")
    add.add_argument("url")
    add.add_argument("--name")
    add.add_argument("--folder", help="folder name or id")
    add.add_argument("--interval", type=parse_duration, help="e.g. 15m, 2h, 1d (minimum 1m)")
    add.add_argument("--method", choices=["auto", "static", "browser", "screenshot"])
    add.add_argument("--keywords", help="keyword rules, one per line (\\n separated)")
    add.add_argument("--private", action="store_true", help="alerts never include page content")

    ls = sub.add_parser("list", help="list bookmarks")
    ls.add_argument("--folder")
    ls.add_argument(
        "--status", choices=["new", "ok", "changed", "error", "needs_login", "disabled"]
    )
    ls.add_argument("--unread", action="store_true")
    ls.add_argument("--query", "-q")

    chk = sub.add_parser("check", help="check bookmarks now")
    chk.add_argument("ids", nargs="*", type=int)
    chk.add_argument("--all", action="store_true")
    chk.add_argument("--folder")
    chk.add_argument("--force", action="store_true", help="skip conditional GET")

    rd = sub.add_parser("read", help="mark bookmarks read")
    rd.add_argument("ids", nargs="*", type=int)
    rd.add_argument("--all", action="store_true", help="every unread bookmark")

    for name, text in (("pause", "pause AutoWatch, or disable bookmarks"),
                       ("resume", "resume AutoWatch, or enable bookmarks")):  # fmt: skip
        sp = sub.add_parser(name, help=text)
        sp.add_argument("ids", nargs="*", type=int, help="bookmark ids (none = AutoWatch itself)")
        if name == "pause":
            sp.add_argument("--for", dest="duration", type=parse_duration, help="e.g. 1h")

    rm = sub.add_parser("rm", help="delete bookmarks")
    rm.add_argument("ids", nargs="+", type=int)

    fo = sub.add_parser("folder", help="manage folders")
    fsub = fo.add_subparsers(dest="folder_cmd", required=True)
    fadd = fsub.add_parser("add")
    fadd.add_argument("name")
    fadd.add_argument("--parent")
    fsub.add_parser("list")
    frm = fsub.add_parser("rm")
    frm.add_argument("folder")

    sub.add_parser("status", help="engine health")
    return p


# -- helpers ----------------------------------------------------------------------------


def _folder_id(client: EngineClient, ref: str | None) -> int | None:
    if ref is None:
        return None
    if ref.isdigit():
        return int(ref)
    matches = [f for f in client.get("/folders") if f["name"].lower() == ref.lower()]
    if not matches:
        raise ApiError(404, f"no folder named {ref!r}")
    if len(matches) > 1:
        raise ApiError(409, f"{len(matches)} folders are named {ref!r}; use the id")
    return int(matches[0]["id"])


def _fmt_time(value: str | None) -> str:
    if not value:
        return "-"
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_interval(secs: int | None) -> str:
    if not secs:
        return "-"
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def _table(rows: list[dict[str, Any]]) -> str:
    cols = [("ID", "id"), ("STATUS", "status"), ("U", "u"), ("NAME", "name"),
            ("EVERY", "every"), ("LAST CHANGED", "changed"), ("NEXT CHECK", "next")]  # fmt: skip
    table = [[c[0] for c in cols]]
    for r in rows:
        table.append([str(r[k]) for _, k in cols])
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in table
    )


def _summary_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": b["id"],
            "status": b["status"],
            "u": "*" if b["unread"] else "",
            "name": b["name"][:40],
            "every": _fmt_interval(b["interval_s"]),
            "changed": _fmt_time(b["last_changed_at"]),
            "next": _fmt_time(b["next_due_at"]),
        }
        for b in items
    ]


# -- commands ---------------------------------------------------------------------------


def run(args: argparse.Namespace, client: EngineClient) -> Any:
    cmd = args.cmd
    if cmd == "add":
        body: dict[str, Any] = {"url": args.url}
        if args.name:
            body["name"] = args.name
        if args.folder:
            body["folder_id"] = _folder_id(client, args.folder)
        if args.interval:
            body["schedule"] = {"interval_s": args.interval}
        if args.method:
            body["check_method"] = args.method
        if args.keywords:
            body["gate"] = {"keywords": args.keywords.replace("\\n", "\n")}
        if args.private:
            body["actions"] = {"actions": [{"type": "toast"}], "alert_privacy": "private"}
        return client.post("/bookmarks", body)
    if cmd == "list":
        params: dict[str, Any] = {"status": args.status, "q": args.query}
        if args.folder:
            params["folder"] = _folder_id(client, args.folder)
        if args.unread:
            params["unread"] = "true"
        return client.iter_bookmarks(**params)
    if cmd == "check":
        body = {"force": args.force}
        if args.all:
            body["all"] = True
        elif args.ids:
            body["ids"] = args.ids
        elif args.folder:
            body["folder_id"] = _folder_id(client, args.folder)
        else:
            raise ApiError(2, "give bookmark ids, --folder or --all")
        return client.post("/check", body)
    if cmd == "read":
        ids = args.ids
        if args.all:
            ids = [b["id"] for b in client.iter_bookmarks(unread="true")]
        if not ids:
            raise ApiError(2, "give bookmark ids or --all")
        for bid in ids:
            client.post(f"/bookmarks/{bid}/read")
        return {"marked_read": len(ids)}
    if cmd == "pause":
        if args.ids:
            return client.post("/bookmarks/bulk", {"ids": args.ids, "action": "disable"})
        until = None
        if args.duration:
            until = (datetime.now(UTC) + timedelta(seconds=args.duration)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        return client.post("/autowatch", {"state": "paused", "until": until})
    if cmd == "resume":
        if args.ids:
            return client.post("/bookmarks/bulk", {"ids": args.ids, "action": "enable"})
        return client.post("/autowatch", {"state": "running"})
    if cmd == "rm":
        return client.post("/bookmarks/bulk", {"ids": args.ids, "action": "delete"})
    if cmd == "folder":
        if args.folder_cmd == "add":
            return client.post(
                "/folders", {"name": args.name, "parent_id": _folder_id(client, args.parent)}
            )
        if args.folder_cmd == "list":
            return client.get("/folders")
        fid = _folder_id(client, args.folder)
        client.delete(f"/folders/{fid}")
        return {"deleted": fid}
    if cmd == "status":
        return client.get("/health")
    raise ApiError(2, f"unknown command {cmd}")


def render(args: argparse.Namespace, result: Any) -> str:
    if args.json:
        return json.dumps(result, indent=2)
    cmd = args.cmd
    if cmd == "list":
        return _table(_summary_rows(result)) if result else "(no bookmarks)"
    if cmd == "add":
        return f"added #{result['id']} {result['name']} ({result['url']})"
    if cmd == "check":
        return f"queued {result['queued']} check(s)"
    if cmd == "read":
        return f"marked {result['marked_read']} bookmark(s) read"
    if cmd in ("pause", "resume") and isinstance(result, dict) and "state" in result:
        until = f" until {_fmt_time(result['until'])}" if result.get("until") else ""
        return f"AutoWatch {result['state']}{until}"
    if cmd in ("pause", "resume", "rm"):
        return f"updated {result['affected']} bookmark(s)"
    if cmd == "folder":
        if args.folder_cmd == "list":
            return (
                "\n".join(
                    f"{f['id']:>4}  {f['name']}"
                    + (f"  (in {f['parent_id']})" if f["parent_id"] else "")
                    for f in result
                )
                or "(no folders)"
            )
        if args.folder_cmd == "add":
            return f"added folder #{result['id']} {result['name']}"
        return f"deleted folder #{result['deleted']}"
    if cmd == "status":
        a = result["autowatch"]
        return (
            f"PageWatch {result['version']} (pid {result['pid']}) up {int(result['uptime_s'])}s\n"
            f"bookmarks {result['bookmarks']}  queued {result['queue_length']}  "
            f"running {result['in_flight']}  autowatch {a['state']}  rss {result['rss_mb']} MB\n"
            f"last 24h: {result['outcomes_24h'] or 'no checks yet'}"
        )
    return json.dumps(result)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with EngineClient(resolve_data_dir(args.data_dir)) as client:
            result = run(args, client)
    except EngineNotRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except ApiError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 2 if exc.status in (2, 404, 409, 422) else 1
    print(render(args, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
