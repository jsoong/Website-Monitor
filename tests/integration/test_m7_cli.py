"""M7: the CLI's power commands, against a real engine API."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.cli.main import build_parser, main
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle
from tests.unit.test_portability import CHROME

SPEC_COMMANDS = {
    "add", "list", "check", "pause", "resume", "read", "report", "import", "export",
    "backup", "restore", "service",
}  # fmt: skip
OWNED_BY_M5 = {"backup", "restore", "service"}  # unattended operation: its own milestone and branch


async def cli(data_dir: DataDir, *args: str) -> tuple[int, str, str]:
    """Run the CLI in a thread (it is synchronous; the engine shares this event loop)."""

    def go() -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(data_dir.root), *args])
        return code, out.getvalue(), err.getvalue()

    return await asyncio.to_thread(go)


def commands() -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        if action.dest == "cmd":
            return set(action.choices or {})  # type: ignore[arg-type]
    raise AssertionError("no sub-commands")


def test_the_cli_has_every_command_of_the_spec_that_this_milestone_owns() -> None:
    have = commands()
    missing = SPEC_COMMANDS - have
    assert missing <= OWNED_BY_M5, f"missing from the CLI: {sorted(missing - OWNED_BY_M5)}"
    assert {"report", "import", "export", "plugin"} <= have
    assert {"add", "list", "check", "pause", "resume", "read"} <= have


# -- report -------------------------------------------------------------------------------------


async def test_report_lists_prints_and_writes_files(
    data_dir: DataDir, client: httpx.AsyncClient, engine: Engine, clock: FakeClock,
    site: FixtureSite, tmp_path: Path,
) -> None:  # fmt: skip
    site.set("/n", article("one"))
    code, out, _ = await cli(data_dir, "add", site.url("/n"), "--name", "News", "--interval", "1m")
    assert code == 0
    await settle(engine, clock)
    site.set("/n", article("one", "two"))
    await advance(engine, clock, 70, step=10)

    code, out, _ = await cli(data_dir, "report", "--list")
    assert code == 0 and "changed" in out and "built in" in out and "html, csv, json, xml" in out
    code, out, _ = await cli(data_dir, "report")  # no name: the same list
    assert "errors" in out

    target = tmp_path / "changes.csv"  # the extension picks the format
    code, out, _ = await cli(data_dir, "report", "changed", "--out", str(target), "--no-mark")
    assert code == 0 and "wrote" in out and str(target) in out
    assert target.read_text().splitlines()[0].startswith("Detected,Bookmark,URL")
    assert "News" in target.read_text()

    code, out, _ = await cli(data_dir, "report", "changed", "--format", "json", "--no-mark")
    assert code == 0 and json.loads(out)["counts"]["rows"] == 1  # printed, not written
    code, out, _ = await cli(data_dir, "--json", "report", "all", "-o", str(tmp_path / "all.html"))
    assert code == 0 and json.loads(out)["bytes"] > 100
    assert "<h1>All bookmarks</h1>" in (tmp_path / "all.html").read_text()

    # a report that marks moves "since the last report"; a --no-mark one never does
    code, out, _ = await cli(data_dir, "report", "changed", "--format", "json")
    assert json.loads(out)["counts"]["rows"] == 1
    code, out, _ = await cli(data_dir, "report", "changed", "--format", "json", "--no-mark")
    assert json.loads(out)["counts"]["rows"] == 0
    code, out, _ = await cli(
        data_dir, "report", "changed", "--format", "json", "--since", "2000-01-01T00:00:00.000000Z"
    )
    assert json.loads(out)["counts"]["rows"] == 1

    code, _, err = await cli(data_dir, "report", "nonesuch")
    assert code == 2 and "no report named" in err
    code, _, err = await cli(data_dir, "report", "all", "--folder", "Nowhere")
    assert code == 2 and "no folder named" in err


# -- import / export ----------------------------------------------------------------------------


async def test_import_and_export_round_trip_on_the_command_line(
    data_dir: DataDir, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    marks = tmp_path / "bookmarks.html"
    marks.write_text(CHROME, encoding="utf-8")
    code, out, _ = await cli(data_dir, "import", str(marks), "--dry-run")
    assert code == 0 and out.startswith("would import 4 bookmark(s) from netscape")
    assert (await client.get("/bookmarks")).json()["total"] == 0
    code, out, _ = await cli(data_dir, "import", str(marks))
    assert code == 0 and out.startswith("imported 4") and "2 folder(s) created" in out
    code, out, _ = await cli(data_dir, "import", str(marks))
    assert "4 already existed" in out

    for name, needle in (("all.json", '"format": "pagewatch-export"'), ("feeds.opml", "<opml"),
                         ("list.csv", "id,name,url")):  # fmt: skip
        code, out, _ = await cli(data_dir, "export", str(tmp_path / name))
        assert code == 0 and "wrote" in out
        assert needle in (tmp_path / name).read_text(encoding="utf-8-sig")
    code, out, _ = await cli(
        data_dir, "--json", "export", str(tmp_path / "x.txt"), "--format", "csv"
    )
    assert json.loads(out)["format"] == "csv"
    ids = [b["id"] for b in (await client.get("/bookmarks")).json()["items"]][:2]
    code, _, _ = await cli(data_dir, "export", str(tmp_path / "two.json"), *map(str, ids))
    assert code == 0
    assert (tmp_path / "two.json").read_text().count('"url"') == 2
    code, _, _ = await cli(data_dir, "export", str(tmp_path / "folder.json"), "--folder", "News")
    assert code == 0 and (tmp_path / "folder.json").read_text().count('"url"') == 1

    # a csv with a bad row: the good one still imports, the bad one is reported
    bad = tmp_path / "bad.csv"
    bad.write_text("name,url\nFine,https://fine.example.com\nBroken,not-a-url\n", encoding="utf-8")
    code, out, _ = await cli(data_dir, "import", str(bad), "--folder", "News")
    assert code == 0 and "imported 1" in out and "row 3:" in out
    code, _, err = await cli(data_dir, "import", str(tmp_path / "missing.csv"))
    assert code == 2 and "cannot read" in err


# -- plugins ------------------------------------------------------------------------------------


async def test_plugins_can_be_listed_and_installed(
    data_dir: DataDir, client: httpx.AsyncClient
) -> None:
    code, out, _ = await cli(data_dir, "plugin", "list")
    assert code == 0
    for name in ("price_range", "stale_page", "single_new_item", "pdf_links"):
        assert name in out
    assert "available" in out and "installed" not in out.replace("available", "")
    code, out, _ = await cli(data_dir, "plugin", "install", "price_range")
    assert code == 0 and out.strip() == "installed price_range (hooks: check_keywords)"
    code, out, _ = await cli(data_dir, "plugin", "list")
    assert (
        "installed"
        in out.splitlines()[[ln.split()[0] for ln in out.splitlines()].index("price_range")]
    )
    code, _, err = await cli(data_dir, "plugin", "install", "price_range")
    assert code == 2 and "already installed" in err
    code, out, _ = await cli(data_dir, "plugin", "install", "price_range", "--overwrite")
    assert code == 0
    code, _, err = await cli(data_dir, "plugin", "install", "ghost")
    assert code == 2 and "no shipped plugin" in err


# -- add and folders ----------------------------------------------------------------------------


async def test_add_takes_a_plugin_follow_links_and_merge_pages(
    data_dir: DataDir, client: httpx.AsyncClient
) -> None:
    await cli(data_dir, "plugin", "install", "pdf_links")
    code, out, _ = await cli(
        data_dir, "add", "https://example.com/hub", "--plugin", "pdf_links", "--follow",
        "--follow-include", "/news/*", "--follow-include", "/blog/*", "--follow-exclude", "*.pdf",
        "--follow-depth", "2", "--follow-pages", "10", "--follow-any-host",
        "--merge-next", "a.next", "--merge-pages", "3",
    )  # fmt: skip
    assert code == 0, out
    b = (await client.get("/bookmarks/1")).json()
    assert b["plugin"] == "pdf_links"
    assert b["fetch"]["follow"] == {
        "enabled": True, "include": ["/news/*", "/blog/*"], "exclude": ["*.pdf"],
        "pattern_kind": "wildcard", "depth": 2, "max_pages": 10, "same_host": False,
        "folder_name": None,
    }  # fmt: skip
    assert (
        b["fetch"]["merge"]["next_selector"] == "a.next" and b["fetch"]["merge"]["max_pages"] == 3
    )

    code, _, _ = await cli(
        data_dir, "add", "https://example.com/p", "--merge-pattern", "https://example.com/p?page={n}",
        "--follow", "--follow-regex", "--follow-include", r"/\d+$",
    )  # fmt: skip
    assert code == 0
    b2 = (await client.get("/bookmarks/2")).json()
    assert b2["fetch"]["merge"]["url_pattern"].endswith("{n}")
    assert b2["fetch"]["follow"]["pattern_kind"] == "regex"

    for args, needle in [
        (["--follow-include", "/x"], "need --follow"),
        (["--merge-next", "a", "--merge-pattern", "/p/{n}"], "not both"),
        (["--merge-pattern", "/p/next"], "{n}"),
        (["--plugin", "ghost"], "not installed"),
    ]:
        code, _, err = await cli(data_dir, "add", "https://example.com/x", *args)
        assert code == 2 and needle in err, (args, err)


async def test_saved_searches_from_the_command_line(
    data_dir: DataDir, client: httpx.AsyncClient
) -> None:
    await cli(data_dir, "add", "https://news.example.com/a", "--name", "Daily news")
    await cli(data_dir, "add", "https://shop.example.com/b", "--name", "Shop")
    code, out, _ = await cli(
        data_dir, "folder", "add", "News only", "--virtual", "--query", '{"q": "news"}'
    )
    assert code == 0 and "added folder" in out
    code, out, _ = await cli(data_dir, "folder", "list")
    assert "News only  [saved search]" in out
    code, out, _ = await cli(data_dir, "--json", "list", "--folder", "News only")
    assert [b["name"] for b in json.loads(out)] == ["Daily news"]
    code, _, err = await cli(data_dir, "folder", "add", "Broken", "--virtual")
    assert code == 2 and "needs --query" in err
    code, _, err = await cli(data_dir, "folder", "add", "Broken", "--query", '{"q": "x"}')
    assert code == 2 and "only goes with --virtual" in err
    code, _, err = await cli(data_dir, "folder", "add", "Broken", "--virtual", "--query", "{}")
    assert code == 2 and "criterion" in err
    with pytest.raises(SystemExit):
        build_parser().parse_args(["folder", "add", "x", "--virtual", "--query", "[1]"])


def test_json_arguments_must_be_objects() -> None:
    parser = build_parser()
    ns: Any = parser.parse_args(["folder", "add", "x", "--virtual", "--query", '{"q": "a"}'])
    assert ns.query == {"q": "a"} and ns.virtual
