"""M7: import and export through the real API, including a full round trip."""

from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from tests.support.docs import make_xlsx
from tests.unit.test_portability import CHROME


async def imp(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    r = await client.post("/import", json=body)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def listing(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await client.get("/bookmarks", params={"limit": 500})).json()[
        "items"
    ]
    return out


async def folder_names(client: httpx.AsyncClient) -> dict[int, str]:
    return {f["id"]: f["name"] for f in (await client.get("/folders")).json()}


# -- import ---------------------------------------------------------------------------------


async def test_browser_bookmarks_become_bookmarks_in_folders(client: httpx.AsyncClient) -> None:
    res = await imp(client, content=CHROME, filename="bookmarks.html")
    assert res["format"] == "netscape" and res["created"] == 4 and not res["errors"]
    assert res["folders_created"] == 2  # "Bookmarks bar" and "News" inside it
    folders = (await client.get("/folders")).json()
    bar = next(f for f in folders if f["name"] == "Bookmarks bar")
    news = next(f for f in folders if f["name"] == "News")
    assert bar["parent_id"] is None and news["parent_id"] == bar["id"]
    by_url = {b["url"]: b for b in await listing(client)}
    assert by_url["https://news.example.com/"]["folder_id"] == news["id"]
    assert by_url["https://example.com/a"]["name"] == "Alpha & Omega"
    assert by_url["https://top.example.com/"]["folder_id"] is None


async def test_importing_the_same_file_twice_adds_nothing(client: httpx.AsyncClient) -> None:
    first = await imp(client, content=CHROME)
    again = await imp(client, content=CHROME)
    assert (again["created"], again["skipped_duplicates"], again["folders_created"]) == (0, 4, 0)
    assert len(await listing(client)) == first["created"] == 4


async def test_duplicates_are_found_by_normalised_url_inside_one_file_too(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/bookmarks", json={"url": "https://Example.com/page/"})
    text = "https://example.com/page\nhttps://example.com/other#top\nhttps://example.com/other/\n"
    res = await imp(client, content=text, format="urls")
    assert (res["created"], res["skipped_duplicates"]) == (1, 2)


async def test_a_dry_run_reports_without_creating(client: httpx.AsyncClient) -> None:
    res = await imp(client, content=CHROME, dry_run=True)
    assert res["dry_run"] and res["created"] == 4 and res["folders_created"] == 2
    assert res["bookmark_ids"] == []
    assert await listing(client) == [] and (await client.get("/folders")).json() == []


async def test_bad_rows_are_reported_and_the_rest_import(client: httpx.AsyncClient) -> None:
    csv_text = (
        "name,url,folder,interval,keywords\n"
        "Good,https://good.example.com,Shops,30m,sale\n"
        "Bad URL,ftp:/missing,,,\n"
        "Bad interval,https://slow.example.com,,10,\n"
        'Bad keyword,https://kw.example.com,,,"foo + ""unterminated"\n'
        ",,,,\n"
        "Fine,https://fine.example.com,,,\n"
    )
    res = await imp(client, content=csv_text, format="csv")
    assert res["created"] == 2
    assert {e["row"] for e in res["errors"]} == {3, 4, 5}
    messages = {e["row"]: e["message"] for e in res["errors"]}
    assert "url" in messages[3] and "minute" in messages[4]
    good = next(b for b in await listing(client) if b["name"] == "Good")
    detail = (await client.get(f"/bookmarks/{good['id']}")).json()
    assert detail["schedule"]["interval_s"] == 1800 and detail["gate"]["keywords"] == "sale"
    assert (await folder_names(client))[good["folder_id"]] == "Shops"


async def test_xlsx_is_sent_as_base64(client: httpx.AsyncClient) -> None:
    data = make_xlsx({"S": [["name", "url"], ["A", "https://a.example.com"]]})
    res = await imp(client, content_base64=base64.b64encode(data).decode(), filename="x.xlsx")
    assert res["format"] == "xlsx" and res["created"] == 1
    r = await client.post("/import", json={"content_base64": "%%%not base64%%%"})
    assert r.status_code == 422 and "base64" in r.text


async def test_import_under_a_folder_and_other_refusals(client: httpx.AsyncClient) -> None:
    root = (await client.post("/folders", json={"name": "Inbox"})).json()["id"]
    res = await imp(client, content=CHROME, folder_id=root)
    names = await folder_names(client)
    bar = next(f for f in (await client.get("/folders")).json() if f["name"] == "Bookmarks bar")
    assert bar["parent_id"] == root and res["created"] == 4 and names[root] == "Inbox"
    top = next(b for b in await listing(client) if b["url"] == "https://top.example.com/")
    assert top["folder_id"] == root  # loose bookmarks land in the chosen folder

    saved = (
        await client.post("/folders", json={"name": "S", "is_virtual": True, "query": {"q": "x"}})
    ).json()["id"]
    for body, needle in [
        ({"content": "x", "folder_id": 999}, "does not exist"),
        ({"content": "x", "folder_id": saved}, "saved search"),
        ({"content": "x", "content_base64": "eA=="}, "exactly one"),
        ({}, "exactly one"),
        ({"content": "a,b\n1,2", "format": "csv"}, "'url' column"),
        ({"content": "{}", "format": "json"}, "not a PageWatch export"),
        ({"content": "x", "format": "xlsx"}, "content_base64"),
    ]:
        r = await client.post("/import", json=body)
        assert r.status_code == 422 and needle in r.text, (body, r.text)


# -- export ---------------------------------------------------------------------------------


async def seed(client: httpx.AsyncClient) -> dict[str, int]:
    news = (await client.post("/folders", json={"name": "News"})).json()["id"]
    tech = (
        await client.post(
            "/folders", json={"name": "Tech", "parent_id": news, "defaults": {"priority": 1}}
        )
    ).json()["id"]
    a = await client.post(
        "/bookmarks",
        json={
            "url": "https://example.com/a", "name": "Alpha", "folder_id": tech,
            "schedule": {"interval_s": 900}, "gate": {"keywords": "price\nsale"},
            "filter": {"ignore": [{"type": "text", "pattern": r"\d+ views",
                                   "pattern_kind": "regex"}]},
            "fetch": {"auth": {"kind": "basic", "username": "me", "secret_key": "pw-1"}},
            "actions": {"actions": [{"type": "toast"}], "alert_privacy": "private"},
            "info1": "note", "highlight_mode": "table", "priority": 1, "enabled": False,
        },
    )  # fmt: skip
    assert a.status_code == 201, a.text
    f = await client.post(
        "/bookmarks",
        json={"url": "https://blog.example.com/feed?a=1&b=2", "name": "Blog <&> feed",
              "source_type": "feed", "folder_id": news},
    )  # fmt: skip
    b = await client.post("/bookmarks", json={"url": "https://example.com/plain"})
    return {"news": news, "tech": tech, "a": a.json()["id"], "feed": f.json()["id"],
            "plain": b.json()["id"]}  # fmt: skip


async def test_json_export_is_versioned_complete_and_free_of_secrets(
    client: httpx.AsyncClient,
) -> None:
    ids = await seed(client)
    r = await client.get("/export", params={"format": "json"})
    assert r.status_code == 200 and "pagewatch-export.json" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["format"] == "pagewatch-export" and doc["version"] == 1 and doc["app_version"]
    assert {f["name"] for f in doc["folders"]} == {"News", "Tech"}
    assert next(f for f in doc["folders"] if f["name"] == "Tech")["defaults"] == {"priority": 1}
    alpha = next(b for b in doc["bookmarks"] if b["name"] == "Alpha")
    assert alpha["schedule"] == {"interval_s": 900}  # stored overrides only, not the inherited rest
    assert alpha["gate"] == {"keywords": "price\nsale"} and alpha["enabled"] is False
    assert alpha["fetch"]["auth"]["secret_key"] == "pw-1"  # the keyring entry's name ...
    assert "password" not in r.text.lower()  # ... and there is no place for the secret itself
    assert alpha["actions"]["alert_privacy"] == "private" and alpha["highlight_mode"] == "table"
    assert ids["plain"] and len(doc["bookmarks"]) == 3


async def test_a_json_export_imports_back_unchanged(client: httpx.AsyncClient) -> None:
    ids = await seed(client)
    export = (await client.get("/export")).text
    before = {
        b["name"]: (await client.get(f"/bookmarks/{b['id']}")).json() for b in await listing(client)
    }
    folders_before = {f["name"]: f for f in (await client.get("/folders")).json()}
    # wipe everything, then import into the empty engine
    await client.post("/bookmarks/bulk", json={"ids": list(before_ids(before)), "action": "delete"})
    for fid in (ids["news"],):
        await client.delete(f"/folders/{fid}")
    assert await listing(client) == [] and (await client.get("/folders")).json() == []

    res = await imp(client, content=export, filename="backup.json")
    assert res["format"] == "json" and res["created"] == 3 and not res["errors"]
    assert res["folders_created"] == 2
    after = {
        b["name"]: (await client.get(f"/bookmarks/{b['id']}")).json() for b in await listing(client)
    }
    assert set(after) == set(before)
    for name, old in before.items():
        new = after[name]
        for key in ("url", "source_type", "check_method", "enabled", "priority", "highlight_mode",
                    "overrides", "schedule", "gate", "filter", "fetch", "actions", "info1"):  # fmt: skip
            assert new[key] == old[key], (name, key)
    folders_after = {f["name"]: f for f in (await client.get("/folders")).json()}
    assert folders_after["Tech"]["defaults"] == folders_before["Tech"]["defaults"]
    assert folders_after["Tech"]["parent_id"] == folders_after["News"]["id"]
    alpha = next(b for b in await listing(client) if b["name"] == "Alpha")
    assert alpha["folder_id"] == folders_after["Tech"]["id"]
    again = await imp(client, content=export)  # idempotent: everything is a duplicate now
    assert (again["created"], again["skipped_duplicates"]) == (0, 3)


def before_ids(before: dict[str, Any]) -> list[int]:
    return [int(b["id"]) for b in before.values()]


async def test_export_can_be_limited_to_a_folder_or_to_ids(client: httpx.AsyncClient) -> None:
    ids = await seed(client)
    doc = (await client.get("/export", params={"folder": ids["tech"]})).json()
    assert [b["name"] for b in doc["bookmarks"]] == ["Alpha"]
    assert [f["name"] for f in doc["folders"]] == ["Tech"] and doc["folders"][0][
        "parent_id"
    ] is None
    doc = (await client.get("/export", params={"ids": [ids["plain"], ids["a"]]})).json()
    assert sorted(b["name"] for b in doc["bookmarks"]) == ["Alpha", "example.com"]
    assert {f["name"] for f in doc["folders"]} == {"News", "Tech"}  # the tree up to Alpha
    assert (await client.get("/export", params={"format": "xml"})).status_code == 422


async def test_opml_lists_feeds_grouped_by_folder_and_is_valid_xml(
    client: httpx.AsyncClient,
) -> None:
    await seed(client)
    r = await client.get("/export", params={"format": "opml"})
    assert r.headers["content-type"].startswith("text/x-opml")
    root = ET.fromstring(r.content)  # escaping of < & > in names and URLs must hold up
    assert root.tag == "opml"
    (group,) = root.find("body").findall("outline")  # type: ignore[union-attr]
    assert group.get("text") == "News"
    (feed,) = group.findall("outline")
    assert feed.get("type") == "rss" and feed.get("text") == "Blog <&> feed"
    assert feed.get("xmlUrl") == "https://blog.example.com/feed?a=1&b=2"


async def test_csv_export_reads_back_through_the_csv_import(client: httpx.AsyncClient) -> None:
    ids = await seed(client)
    r = await client.get("/export", params={"format": "csv"})
    assert r.content.startswith(b"\xef\xbb\xbf")  # a BOM, for Excel
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("id,name,url,folder,interval,keywords,enabled,status")
    await client.post("/bookmarks/bulk", json={"ids": list(ids.values())[2:], "action": "delete"})
    res = await imp(client, content=r.content.decode("utf-8-sig"), format="csv")
    assert res["created"] == 3 and not res["errors"]
    alpha = next(b for b in await listing(client) if b["name"] == "Alpha")
    detail = (await client.get(f"/bookmarks/{alpha['id']}")).json()
    assert detail["schedule"]["interval_s"] == 900 and detail["gate"]["keywords"] == "price\nsale"
    assert (await folder_names(client))[alpha["folder_id"]] == "Tech"
    assert json.loads(json.dumps(res))["errors"] == []


async def test_a_bookmark_whose_plugin_is_missing_is_a_reported_row_not_a_silent_loss(
    client: httpx.AsyncClient, data_dir: Any
) -> None:
    (data_dir.plugins_dir / "mine.py").write_text("def compare(ctx, old, new): return None\n")
    made = await client.post("/bookmarks", json={"url": "https://example.com/p", "plugin": "mine"})
    export = (await client.get("/export")).text
    await client.delete(f"/bookmarks/{made.json()['id']}")
    (data_dir.plugins_dir / "mine.py").unlink()  # "this machine" does not have the plugin
    res = await imp(client, content=export)
    assert res["created"] == 0 and len(res["errors"]) == 1
    assert "plugin 'mine' is not installed" in res["errors"][0]["message"]
    (data_dir.plugins_dir / "mine.py").write_text("def compare(ctx, old, new): return None\n")
    assert (await imp(client, content=export))["created"] == 1  # install it and import again
