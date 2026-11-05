"""M7: reports through the real API, and the report action."""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.report import expand
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle

SCHED = {"interval_s": 60, "jitter_pct": 0}


async def add(client: httpx.AsyncClient, url: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post("/bookmarks", json={"url": url, **kw})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def report(client: httpx.AsyncClient, name: str, **body: Any) -> httpx.Response:
    return await client.post(f"/reports/{name}", json=body)


async def world(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> dict[str, int]:
    """Three bookmarks: one that changed, one that is steady, one that fails."""
    site.set("/news", article("one"))
    site.set("/steady", article("same"))
    site.set("/broken", "x", status=500)
    folder = (await client.post("/folders", json={"name": "Shops"})).json()["id"]
    ids = {
        "news": await add(client, site.url("/news"), name="News & Views", folder_id=folder),
        "steady": await add(client, site.url("/steady"), name="Steady"),
        "broken": await add(
            client, site.url("/broken"), name="Broken", gate={"error_threshold": 1}
        ),  # fmt: skip
        "folder": folder,
    }
    await settle(engine, clock)
    site.set("/news", article("one", "two <b>new</b> & shiny"))
    await advance(engine, clock, 130, step=10)
    return ids


async def test_the_catalogue_lists_builtins_and_user_templates(
    client: httpx.AsyncClient, data_dir: DataDir
) -> None:
    (data_dir.root / "reports").mkdir()
    (data_dir.root / "reports" / "weekly.html.j2").write_text("hi", encoding="utf-8")
    (data_dir.root / "reports" / "weekly.csv.j2").write_text("a", encoding="utf-8")
    (data_dir.root / "reports" / "nonsense.txt.j2").write_text("a", encoding="utf-8")
    (data_dir.root / "reports" / "bad name.html.j2").write_text("a", encoding="utf-8")
    got = {r["name"]: r for r in (await client.get("/reports")).json()}
    assert set(got) == {"changed", "all", "errors", "weekly"}
    assert got["changed"]["builtin"] and got["changed"]["formats"] == ["html", "csv", "json", "xml"]
    assert not got["weekly"]["builtin"] and got["weekly"]["formats"] == ["csv", "html"]


async def test_changed_report_lists_only_what_changed_in_every_format(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await world(client, engine, clock, site)
    js = (await report(client, "changed", format="json", mark=False)).json()
    assert js["title"].startswith("Changed since") and js["counts"]["rows"] == 1
    (row,) = js["rows"]
    assert row["name"] == "News & Views" and "two" in row["summary"] and row["added_words"] > 0

    html = (await report(client, "changed", format="html", mark=False)).text
    assert "<h1>Changed since the last report</h1>" in html
    assert "News &amp; Views" in html and "<b>new</b>" not in html  # escaped, not injected
    assert "Steady" not in html and "Broken" not in html

    rows = list(csv.reader(io.StringIO((await report(client, "changed", format="csv",
                                                      mark=False)).text)))  # fmt: skip
    assert rows[0][:3] == ["Detected", "Bookmark", "URL"] and rows[1][1] == "News & Views"

    root = ET.fromstring((await report(client, "changed", format="xml", mark=False)).content)
    assert root.get("name") == "changed" and root.get("rows") == "1"
    assert root.findtext("item/name") == "News & Views"


async def test_the_since_marker_moves_only_when_asked(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    await world(client, engine, clock, site)
    peek = await report(client, "changed", format="json", mark=False)
    assert peek.json()["counts"]["rows"] == 1
    again = await report(client, "changed", format="json", mark=False)
    assert again.json()["counts"]["rows"] == 1  # a peek changes nothing
    first = await report(client, "changed", format="json")  # marks
    assert first.json()["counts"]["rows"] == 1
    clock.advance(1)
    second = await report(client, "changed", format="json")
    assert second.json()["counts"]["rows"] == 0 and "Nothing" not in second.text
    site.set("/news", article("one", "two", "three"))
    await advance(engine, clock, 70, step=10)
    third = await report(client, "changed", format="json")
    assert third.json()["counts"]["rows"] == 1 and "three" in third.json()["rows"][0]["summary"]
    explicit = await report(client, "changed", format="json", since="2000-01-01T00:00:00.000000Z",
                            mark=False)  # fmt: skip
    assert explicit.json()["counts"]["rows"] == 2  # both alerts, from the start of time
    assert "Nothing to report" in (await report(client, "changed", mark=False)).text


async def test_all_and_errors_reports(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    ids = await world(client, engine, clock, site)
    everything = (await report(client, "all", format="json")).json()
    assert [r["name"] for r in everything["rows"]] == ["News & Views", "Steady", "Broken"]
    news = everything["rows"][0]
    assert news["folder"] == "Shops" and news["unread"] == "yes" and news["interval"] == "1m"
    assert everything["since"] is None
    errors = (await report(client, "errors", format="json")).json()
    (bad,) = errors["rows"]
    assert bad["id"] == ids["broken"] and bad["reason"] == "http_500" and bad["errors"] >= 1
    scoped = (await report(client, "all", format="json", folder_id=ids["folder"])).json()
    assert [r["name"] for r in scoped["rows"]] == ["News & Views"]


async def test_user_templates_see_the_same_data_in_a_sandbox(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir,
) -> None:  # fmt: skip
    await world(client, engine, clock, site)
    folder = data_dir.root / "reports"
    folder.mkdir()
    (folder / "digest.html.j2").write_text(
        "<h1>{{ title }}</h1>{% for c in changes %}<p>{{ c.name }}: {{ c.added_words }}</p>"
        "{% endfor %}<i>{{ counts.bookmarks }}</i>",
        encoding="utf-8",
    )
    (folder / "digest.csv.j2").write_text(
        "name,words\n{% for c in changes %}{{ c.name }},{{ c.added_words }}\n{% endfor %}",
        encoding="utf-8",
    )
    (folder / "evil.html.j2").write_text("{{ ''.__class__.__mro__[1].__subclasses__() }}",
                                         encoding="utf-8")  # fmt: skip
    (folder / "broken.html.j2").write_text("{% for %}", encoding="utf-8")
    r = await report(client, "digest", mark=False)  # the template's own format
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "<p>News &amp; Views: " in r.text and "<i>3</i>" in r.text  # all bookmarks, 1 change
    csv_r = await report(client, "digest", format="csv", mark=False)
    assert csv_r.text.splitlines()[0] == "name,words" and "News & Views," in csv_r.text
    assert (await report(client, "digest", format="xml")).status_code == 422  # no such template
    assert (await report(client, "evil")).status_code == 422  # the sandbox refuses
    assert (await report(client, "broken")).status_code == 422
    assert (await report(client, "missing")).status_code == 404
    assert (await report(client, "changed", format="pdf")).status_code == 422


async def test_a_user_template_can_replace_a_builtin(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite,
    data_dir: DataDir,
) -> None:  # fmt: skip
    await world(client, engine, clock, site)
    folder = data_dir.root / "reports"
    folder.mkdir()
    (folder / "all.html.j2").write_text("custom:{{ rows|length }}", encoding="utf-8")
    assert (await report(client, "all", format="html")).text == "custom:3"
    assert json.loads((await report(client, "all", format="json")).text)["rows"]  # others stay


# -- the action ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "ok"),
    [("out/{name}-{id}.html", True), ("{datetime}.csv", True), ("{date}", True),
     ("{0}", False), ("{name.upper}", False), ("{nope}", False), ("{name!r}", False),
     ("{name:>20}", False)],
)  # fmt: skip
def test_path_templates_allow_only_plain_names(template: str, ok: bool) -> None:
    values = {"id": "1", "name": "n", "date": "d", "datetime": "dt", "template": "t"}
    if ok:
        assert expand(template, values)
    else:
        with pytest.raises(ValueError, match="placeholder"):
            expand(template, values)


async def test_the_report_action_writes_a_file_per_change(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite, tmp_path: Path
) -> None:
    site.set("/news", article("one"))
    out = tmp_path / "reports" / "{name}.json"
    bid = await add(
        client, site.url("/news"), name="Daily/News?",
        actions={"actions": [{"type": "report", "params": {"path": str(out), "format": "json"}}]},
    )  # fmt: skip
    await settle(engine, clock)
    target = tmp_path / "reports" / "Daily_News.json"
    assert not target.exists()  # the first check is not a change
    site.set("/news", article("one", "two"))
    await advance(engine, clock, 70, step=10)
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["counts"]["rows"] == 1 and doc["rows"][0]["bookmark_id"] == bid
    assert not list(target.parent.glob(".*.tmp"))  # atomic: no temp file left behind
    # the action never moves the marker, so a later manual report still sees this change
    manual = await report(client, "changed", format="json", mark=False)
    assert manual.json()["counts"]["rows"] == 1


async def test_a_misconfigured_report_action_fails_at_once_and_is_listed(
    client: httpx.AsyncClient, engine: Engine, clock: FakeClock, site: FixtureSite
) -> None:
    site.set("/news", article("one"))
    bid = await add(client, site.url("/news"),
                    actions={"actions": [{"type": "report", "params": {}}]})  # fmt: skip
    await settle(engine, clock)
    site.set("/news", article("one", "two"))
    await advance(engine, clock, 70, step=10)

    def jobs(conn: Any) -> list[Any]:
        return conn.execute("SELECT status, attempts, last_error FROM action_job").fetchall()

    (job,) = await engine.db.read(jobs)
    assert job["status"] == "failed" and job["attempts"] == 1  # no retries for a config error
    assert "'path'" in job["last_error"] and bid
