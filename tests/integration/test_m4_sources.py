"""M4 acceptance, content sources: records, feeds, documents, local files and FTP, end to end
through the engine under a fake clock."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from tests.support.docs import make_docx, make_pdf, make_xlsx
from tests.support.fixture_site import FixtureSite
from tests.support.ftp_server import serve
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return out  # newest first


async def changes(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/changes")).json()["items"]
    return list(reversed(out))


# -- records ----------------------------------------------------------------------------

REC = {"path": "$.lotteries", "id_field": "lottery_id", "fields": ["name", "status"]}


def lotteries(*rows: tuple[int, str, str]) -> str:
    return json.dumps(
        {"lotteries": [{"lottery_id": i, "name": n, "status": s} for i, n, s in rows]}
    )


S1, S2 = (101, "Sunset Terrace", "Active"), (102, "Harbor View", "Active")
S3 = (103, "Riverside Commons", "Active")


async def test_a_new_lottery_produces_exactly_one_alert_naming_it(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/l.json", lotteries(S1, S2), content_type="application/json")
    bid = await add(client, site.url("/l.json"), name="Lotteries", source_type="records",
                    fetch={"records": REC})  # fmt: skip
    await settle(engine, clock)
    assert not toasts.shown and (await runs(client, bid))[0]["outcome"] == "first"
    assert (await runs(client, bid))[0]["method"] == "records"

    await advance(engine, clock, 130, step=10)  # two unchanged checks
    assert not toasts.shown

    site.set("/l.json", lotteries(S1, S2, S3), content_type="application/json")
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1
    body = toasts.shown[0].body
    assert body.startswith("New: ") and "Riverside Commons" in body and "103" in body
    assert "Sunset Terrace" not in body  # it names what is new, not the whole list
    (change,) = await changes(client, bid)
    assert change["added_words"] > 0 and "Riverside Commons" in change["summary"]

    await advance(engine, clock, 300, step=30)  # steady again: still one alert
    assert len(toasts.shown) == 1


async def test_changed_and_removed_records_are_named_too(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/l.json", lotteries(S1, S2, (104, "Old Lottery", "Active")),
             content_type="application/json")  # fmt: skip
    await add(client, site.url("/l.json"), source_type="records", fetch={"records": REC})
    await settle(engine, clock)
    site.set("/l.json", lotteries(S1, (102, "Harbor View", "Closed")),
             content_type="application/json")  # fmt: skip
    await advance(engine, clock, 70, step=10)
    (toast,) = toasts.shown
    assert "Changed: lottery_id: 102 | name: Harbor View | status: Closed" in toast.body
    assert "Removed: lottery_id: 104 | name: Old Lottery" in toast.body


async def test_each_bookmark_chooses_which_events_alert(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/l.json", lotteries(S1, S2), content_type="application/json")
    bid = await add(client, site.url("/l.json"), source_type="records",
                    fetch={"records": {**REC, "events": ["new"]}})  # fmt: skip
    await settle(engine, clock)
    site.set("/l.json", lotteries(S1, (102, "Harbor View", "Closed")),
             content_type="application/json")  # fmt: skip
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown  # a status change is not a `new` event
    latest = (await runs(client, bid))[0]
    assert (latest["outcome"], latest["reason"]) == ("suppressed", "records_events")
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["latest_version_id"] != b["baseline_version_id"]  # stored, so it is not re-found
    site.set("/l.json", lotteries(S1, (102, "Harbor View", "Closed"), S3),
             content_type="application/json")  # fmt: skip
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Riverside Commons" in toasts.shown[0].body


async def test_an_api_that_changes_shape_fails_the_check_instead_of_removing_every_record(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/l.json", lotteries(S1, S2), content_type="application/json")
    bid = await add(client, site.url("/l.json"), source_type="records", fetch={"records": REC},
                    gate={"error_threshold": 2})  # fmt: skip
    await settle(engine, clock)
    site.set("/l.json", json.dumps({"data": {"items": []}}), content_type="application/json")
    await advance(engine, clock, 200, step=20)
    last = (await runs(client, bid))[0]
    assert last["outcome"] == "error" and last["reason"] == "parse"
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["status"] == "error" and b["latest_version_id"] == b["baseline_version_id"]
    assert len(toasts.shown) == 1 and "matched nothing" in toasts.shown[0].body  # one error toast
    site.set("/l.json", lotteries(S1, S2), content_type="application/json")  # the API recovers
    await advance(engine, clock, 70, step=10)
    assert (await runs(client, bid))[0]["outcome"] == "unchanged"
    assert (await client.get(f"/bookmarks/{bid}")).json()["status"] == "ok"
    assert len(toasts.shown) == 1  # recovery is not a change


async def test_csv_records_and_the_row_filter(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    csv1 = "id,borough,status\n1,MN,Active\n2,BX,Active\n"
    site.set("/d.csv", csv1, content_type="text/csv")
    await add(client, site.url("/d.csv"), source_type="records",
              fetch={"records": {"id_field": "id", "filter": "borough in [MN, BK]"}})  # fmt: skip
    await settle(engine, clock)
    site.set("/d.csv", csv1 + "3,QN,Active\n", content_type="text/csv")  # outside the filter
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown
    site.set("/d.csv", csv1 + "3,QN,Active\n4,BK,Active\n", content_type="text/csv")
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "id: 4 | borough: BK" in toasts.shown[0].body


async def test_a_records_source_needs_its_configuration(
    client: httpx.AsyncClient, site: FixtureSite
) -> None:
    r = await client.post("/bookmarks", json={"url": site.url("/l.json"), "source_type": "records"})
    assert r.status_code == 422 and "records" in r.text
    r = await client.post(
        "/bookmarks",
        json={"url": site.url("/l.json"), "source_type": "records",
              "fetch": {"records": {"id_field": "id", "filter": "status ="}}},
    )  # fmt: skip
    assert r.status_code == 422 and "row filter" in r.text
    bid = await add(client, site.url("/l.json"))
    r = await client.patch(f"/bookmarks/{bid}", json={"source_type": "records"})
    assert r.status_code == 422
    r = await client.patch(
        f"/bookmarks/{bid}", json={"source_type": "records", "fetch": {"records": REC}}
    )
    assert r.status_code == 200


async def test_folder_defaults_can_supply_the_records_configuration(
    client: httpx.AsyncClient, engine: Engine, site: FixtureSite
) -> None:
    f = await client.post("/folders", json={"name": "Housing",
                                            "defaults": {"fetch": {"records": REC}}})  # fmt: skip
    assert f.status_code == 201
    r = await client.post("/bookmarks", json={"url": site.url("/l.json"), "source_type": "records",
                                              "folder_id": f.json()["id"]})  # fmt: skip
    assert r.status_code == 201, r.text


# -- feeds ------------------------------------------------------------------------------


def rss(*items: tuple[str, str, str]) -> str:
    body = "".join(
        f"<item><title>{t}</title><link>https://n.test/{s}</link><guid>{s}</guid>"
        f"<description>{d}</description></item>"
        for t, s, d in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>City News</title>'
        f"<link>https://n.test/</link><description>d</description>{body}</channel></rss>"
    )


async def test_a_new_feed_entry_is_one_alert_and_the_check_is_a_feed_check(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    a = ("Council meets", "a", "Budget vote Tuesday")
    site.set("/feed.xml", rss(a), content_type="application/rss+xml")
    bid = await add(client, site.url("/feed.xml"), name="News")  # auto: found from the content type
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["method"] == "feed"
    site.set("/feed.xml", rss(("Library extends hours", "b", "Open until nine"), a),
             content_type="application/rss+xml")  # fmt: skip
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Library extends hours" in toasts.shown[0].body
    assert (await runs(client, bid))[0]["method"] == "feed"


async def test_enclosures_are_downloaded_once_when_asked(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite, tmp_path: Path
) -> None:
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Pod</title><link>https://p.test</link>'
        "<description>d</description><item><title>Episode 1</title><guid>e1</guid>"
        f'<enclosure url="{site.url("/ep1.mp3")}" length="5" type="audio/mpeg"/></item>'
        "</channel></rss>"
    )
    site.set("/pod.xml", body, content_type="application/rss+xml")
    site.set("/ep1.mp3", b"AUDIO", content_type="audio/mpeg")
    target = tmp_path / "enclosures"
    await add(client, site.url("/pod.xml"), source_type="feed",
              fetch={"feed": {"download_enclosures": True, "enclosures_dir": str(target)}})  # fmt: skip
    await settle(engine, clock)
    files = list(target.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b"AUDIO"
    await advance(engine, clock, 130, step=10)
    assert len(site.hits_for("/ep1.mp3")) == 1  # already saved: not fetched again


async def test_an_unreadable_feed_is_a_failed_check_not_an_empty_page(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/feed.xml", "<html><body>Maintenance</body></html>", content_type="text/html")
    bid = await add(client, site.url("/feed.xml"), source_type="feed")
    await settle(engine, clock)
    last = (await runs(client, bid))[0]
    assert last["outcome"] == "error" and last["reason"] == "parse"


# -- documents over HTTP ----------------------------------------------------------------


HOURS = ["Library opening hours", "Monday to Friday 9 to 5", "Closed on public holidays"]


async def test_a_pdf_revision_highlights_the_changed_paragraph(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/hours.pdf", make_pdf([HOURS]), content_type=PDF)
    bid = await add(client, site.url("/hours.pdf"), name="Hours")
    await settle(engine, clock)
    first = (await runs(client, bid))[0]
    assert first["outcome"] == "first" and first["method"] == "document"

    site.set("/hours.pdf", make_pdf([[HOURS[0], "Monday to Saturday 9 to 6", HOURS[2]]]),
             content_type=PDF)  # fmt: skip
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Saturday" in toasts.shown[0].body

    highlighted = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "highlight"})).text
    # exactly the changed paragraph is marked, word by word; its neighbours are untouched
    assert highlighted.count('<p class="pw-rep-block">') == 1
    assert '<ins class="pw-add">Saturday</ins>' in highlighted
    assert '<del class="pw-del">Friday</del>' in highlighted
    assert "<p>Library opening hours</p>" in highlighted
    assert "<p>Closed on public holidays</p>" in highlighted
    text = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "text"})).text
    assert "Closed on public holidays" in text  # unchanged paragraphs are context
    new = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "new"})).text
    old = (await client.get(f"/bookmarks/{bid}/diff", params={"view": "old"})).text
    assert "Saturday" in new and "Saturday" not in old and "Friday" in old  # PDF text, not bytes
    assert "%PDF" not in new


async def test_a_reissued_pdf_with_the_same_text_is_not_a_change(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    site.set("/hours.pdf", make_pdf([HOURS]), content_type=PDF)
    bid = await add(client, site.url("/hours.pdf"))
    await settle(engine, clock)
    site.set("/hours.pdf", make_pdf([HOURS, []]), content_type=PDF)  # new bytes, same text
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown and (await runs(client, bid))[0]["outcome"] == "unchanged"


async def test_a_broken_document_is_a_parse_error_naming_the_file_type(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/x.pdf", b"%PDF-1.4 truncated nonsense", content_type=PDF)
    bid = await add(client, site.url("/x.pdf"))
    await settle(engine, clock)
    last = (await runs(client, bid))[0]
    assert last["outcome"] == "error" and last["reason"] == "parse"


# -- local files and folders ------------------------------------------------------------


async def test_a_local_word_document_is_watched_by_mtime_and_size_and_content(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, tmp_path: Path,
    toasts: LogToastBackend, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    f = tmp_path / "policy.docx"
    f.write_bytes(make_docx([("h1", "Policy"), ("p", "Badges are required.")]))
    bid = await add(client, f.as_uri(), name="Policy")
    await settle(engine, clock)
    first = (await runs(client, bid))[0]
    assert first["method"] == "file" and first["outcome"] == "first"

    reads: list[Path] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    await advance(engine, clock, 130, step=10)  # unchanged mtime and size: not even read
    assert reads == [] and (await runs(client, bid))[0]["outcome"] == "unchanged"
    assert not toasts.shown

    f.write_bytes(make_docx([("h1", "Policy"), ("p", "Badges are required at all times.")]))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "required at all times" in toasts.shown[0].body
    assert reads  # the changed file was read
    assert (await runs(client, bid))[0]["method"] == "file"


async def test_touching_a_file_without_changing_it_is_read_but_not_a_change(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, tmp_path: Path,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    f = tmp_path / "a.txt"
    f.write_text("steady")
    bid = await add(client, f.as_uri())
    await settle(engine, clock)
    f.write_text("steady")  # a newer mtime, identical bytes
    st = f.stat()
    import os

    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    await advance(engine, clock, 70, step=10)
    assert not toasts.shown and (await runs(client, bid))[0]["outcome"] == "unchanged"


async def test_a_local_spreadsheet_cell_update(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, tmp_path: Path,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    f = tmp_path / "prices.xlsx"
    f.write_bytes(make_xlsx({"P": [["Tea", 3.5], ["Cake", 4]]}))
    await add(client, f.as_uri(), highlight_mode="table")
    await settle(engine, clock)
    f.write_bytes(make_xlsx({"P": [["Tea", 3.5], ["Cake", 4.5]]}))
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "Cake | 4.5" in toasts.shown[0].body


async def test_a_folder_listing_alerts_when_a_file_appears(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, tmp_path: Path,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "agenda.pdf").write_bytes(b"x" * 10)
    bid = await add(client, folder.as_uri(), source_type="folder", name="Inbox")
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["method"] == "file"
    (folder / "budget-2027.pdf").write_bytes(b"y" * 20)
    await advance(engine, clock, 70, step=10)
    assert len(toasts.shown) == 1 and "budget-2027.pdf" in toasts.shown[0].body


async def test_a_missing_file_is_a_404_error_that_recovers_when_it_returns(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, tmp_path: Path
) -> None:
    f = tmp_path / "later.txt"
    bid = await add(client, f.as_uri())
    await settle(engine, clock)
    assert (await runs(client, bid))[0]["reason"] == "http_404"
    f.write_text("here now")
    await advance(engine, clock, 70, step=10)
    assert (await runs(client, bid))[0]["outcome"] == "first"


# -- FTP --------------------------------------------------------------------------------


async def test_an_ftp_file_and_directory_are_watched(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, tmp_path: Path,
    toasts: LogToastBackend,
) -> None:  # fmt: skip
    async with serve(tmp_path / "srv") as ftp:
        (ftp.root / "notice.txt").write_text("office closed monday")
        file_bid = await add(client, ftp.url("/notice.txt"), name="Notice")
        dir_bid = await add(client, ftp.url("/"), source_type="ftp", name="Share")
        await settle(engine, clock)
        assert {(await runs(client, b))[0]["method"] for b in (file_bid, dir_bid)} == {"ftp"}
        (ftp.root / "notice.txt").write_text("office closed monday and tuesday")
        (ftp.root / "newfile.pdf").write_bytes(b"z" * 12)
        await advance(engine, clock, 70, step=10)
        bodies = sorted(t.body for t in toasts.shown)
        assert len(bodies) == 2
        assert any("and tuesday" in b for b in bodies) and any("newfile.pdf" in b for b in bodies)


# -- check_run.method -------------------------------------------------------------------


async def test_the_method_column_names_how_each_source_was_checked(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite, tmp_path: Path
) -> None:
    site.set(
        "/p", "<html><body><p>plain page with enough words to be a normal page</p></body></html>"
    )
    site.set("/d.pdf", make_pdf([["a document"]]), content_type=PDF)
    site.set("/f.xml", rss(("x", "x", "y")), content_type="application/rss+xml")
    site.set("/r.json", lotteries(S1), content_type="application/json")
    (tmp_path / "t.txt").write_text("local")
    ids = {
        "static": await add(client, site.url("/p")),
        "document": await add(client, site.url("/d.pdf")),
        "feed": await add(client, site.url("/f.xml")),
        "records": await add(
            client, site.url("/r.json"), source_type="records", fetch={"records": REC}
        ),  # fmt: skip
        "file": await add(client, (tmp_path / "t.txt").as_uri()),
    }
    await settle(engine, clock)
    for expected, bid in ids.items():
        assert (await runs(client, bid))[0]["method"] == expected, expected
    rows: list[sqlite3.Row] = await engine.db.read(
        lambda c: c.execute("SELECT DISTINCT method FROM check_run").fetchall()
    )
    assert {r["method"] for r in rows} == set(ids)


# -- the CLI ----------------------------------------------------------------------------


async def test_the_cli_adds_a_records_bookmark_with_type_and_fetch_options(
    api: tuple[str, str], client: httpx.AsyncClient, engine: Engine, clock: FakeClock,
    site: FixtureSite, toasts: LogToastBackend, data_dir: Any,
) -> None:  # fmt: skip
    import contextlib
    import io

    from pagewatch.cli.main import main as cli_main

    async def run_cli(*args: str) -> tuple[int, str]:
        import asyncio

        def go() -> tuple[int, str]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    code = cli_main(["--data-dir", str(data_dir.root), *args])
                except SystemExit as exc:  # argparse rejects bad arguments this way
                    code = int(exc.code or 0)
            return code, buf.getvalue()

        return await asyncio.to_thread(go)

    site.set("/l.json", lotteries(S1), content_type="application/json")
    code, out = await run_cli(
        "--json", "add", site.url("/l.json"), "--name", "Lotteries", "--interval", "1m",
        "--type", "records", "--fetch", json.dumps({"records": REC}),
    )  # fmt: skip
    assert code == 0, out
    bid = json.loads(out)["id"]
    b = (await client.get(f"/bookmarks/{bid}")).json()
    assert b["source_type"] == "records" and b["fetch"]["records"]["id_field"] == "lottery_id"
    await settle(engine, clock)
    site.set("/l.json", lotteries(S1, S2), content_type="application/json")
    await advance(engine, clock, 130, step=10)
    assert len(toasts.shown) == 1 and "Harbor View" in toasts.shown[0].body

    code, out = await run_cli("add", site.url("/x"), "--fetch", "[1, 2]")
    assert code != 0 and "JSON object" in out
    code, out = await run_cli("add", site.url("/x"), "--fetch", "{nope")
    assert code != 0 and "not valid JSON" in out
    code, out = await run_cli("add", site.url("/x"), "--type", "records")
    assert code != 0 and "records" in out  # the engine refuses it: no records configuration
