"""Writes the M4 golden-corpus fixtures (tests/fixtures/sites/<case>/): PDF, DOCX, XLSX, RSS and
records pairs. The files are committed; run this only to change a fixture, then review the diff
and regenerate the snapshots with ``UPDATE_GOLDEN=1 pytest tests/unit/test_golden_corpus.py``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support.docs import make_docx, make_pdf, make_xlsx  # noqa: E402

SITES = ROOT / "tests" / "fixtures" / "sites"


def case(
    name: str, ext: str, versions: list[bytes | str], bookmark: dict[str, Any],
    expect: list[dict[str, Any]], description: str, content_type: str | None = None,
) -> None:  # fmt: skip
    d = SITES / name
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"v*.{ext}"):
        old.unlink()
    for n, body in enumerate(versions, start=1):
        data = body.encode() if isinstance(body, str) else body
        (d / f"v{n}.{ext}").write_bytes(data)
    meta: dict[str, Any] = {"description": description, "ext": ext, "bookmark": bookmark,
                            "expect": expect}  # fmt: skip
    if content_type:
        meta["content_type"] = content_type
    (d / "case.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")


PDF, DOCX, XLSX = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# -- documents --------------------------------------------------------------------------

hours = ["Library opening hours", "Monday to Friday 9 to 5", "Closed on public holidays"]
case(
    "pdf-revision", "pdf",
    [make_pdf([hours]), make_pdf([[hours[0], "Monday to Saturday 9 to 6", hours[2]]])],
    {}, [{"outcome": "alert"}],
    "A revised PDF: one paragraph of three changes, and only it is highlighted.", PDF,
)  # fmt: skip
case(
    "pdf-reissued-same-text", "pdf",
    [make_pdf([hours]), make_pdf([hours, []])],
    {}, [{"outcome": "unchanged"}],
    "The PDF is re-issued with an extra blank page: different bytes, same text, no change.", PDF,
)  # fmt: skip
case(
    "pdf-two-pages-new-page", "pdf",
    [make_pdf([hours]), make_pdf([hours, ["Appendix", "Holiday schedule published"]])],
    {}, [{"outcome": "alert"}],
    "A new page is appended to the PDF.", PDF,
)  # fmt: skip
case(
    "docx-paragraph-and-table", "docx",
    [
        make_docx([("h1", "Fee schedule"), ("p", "Fees are reviewed yearly."),
                   ("table", [["Item", "Fee"], ["Permit", "$40"]])]),
        make_docx([("h1", "Fee schedule"), ("p", "Fees are reviewed yearly."),
                   ("table", [["Item", "Fee"], ["Permit", "$45"]])]),
    ],
    {"mode": "table"}, [{"outcome": "alert"}],
    "A table cell in a Word document changes; table mode marks the row.", DOCX,
)  # fmt: skip
case(
    "xlsx-cell-update", "xlsx",
    [
        make_xlsx({"Prices": [["Item", "Price"], ["Tea", 3.5], ["Cake", 4.0], ["Pie", 5.25]]}),
        make_xlsx({"Prices": [["Item", "Price"], ["Tea", 3.5], ["Cake", 4.5], ["Pie", 5.25]]}),
    ],
    {"mode": "table"}, [{"outcome": "alert"}],
    "One price in a spreadsheet changes.", XLSX,
)  # fmt: skip


# -- feeds ------------------------------------------------------------------------------


def rss(items: list[tuple[str, str, str]], built: str) -> str:
    entries = "".join(
        f"<item><title>{t}</title><link>https://news.test/{s}</link><guid>{s}</guid>"
        f"<description>{d}</description><pubDate>{built}</pubDate></item>"
        for t, s, d in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>City News</title><link>https://news.test/</link><description>News</description>"
        f"<lastBuildDate>{built}</lastBuildDate>{entries}</channel></rss>"
    )


a = [("Council approves budget", "budget", "The vote was 7 to 2."),
     ("Road work begins", "roads", "Main Street closes Monday.")]  # fmt: skip
RSS = "application/rss+xml"
case(
    "rss-new-item", "xml",
    [rss(a, "Mon, 05 Oct 2026 10:00:00 GMT"),
     rss([("Library extends hours", "library", "Open until 9 pm."), *a],
         "Mon, 05 Oct 2026 11:00:00 GMT")],
    {}, [{"outcome": "alert"}],
    "A new entry appears at the top of the feed.", RSS,
)  # fmt: skip
case(
    "rss-rebuilt-without-changes", "xml",
    [rss(a, "Mon, 05 Oct 2026 10:00:00 GMT"), rss(a, "Tue, 06 Oct 2026 10:00:00 GMT")],
    {}, [{"outcome": "unchanged"}],
    "Every publication date is bumped but no entry changed: not a change.", RSS,
)  # fmt: skip
case(
    "rss-entry-edited", "xml",
    [rss(a, "Mon, 05 Oct 2026 10:00:00 GMT"),
     rss([a[0], ("Road work begins", "roads", "Main Street closes Tuesday instead.")],
         "Mon, 05 Oct 2026 12:00:00 GMT")],
    {}, [{"outcome": "alert"}],
    "The summary of an existing entry is corrected.", RSS,
)  # fmt: skip
case(
    "rss-oldest-entry-drops-off", "xml",
    [rss(a, "Mon, 05 Oct 2026 10:00:00 GMT"), rss(a[:1], "Mon, 05 Oct 2026 11:00:00 GMT")],
    {"gate": {"ignore_removed": True}}, [{"outcome": "suppressed", "reason": "removed_only"}],
    "A feed that keeps N entries loses its oldest one; with ignore-removed that is not an alert.",
    RSS,
)  # fmt: skip


# -- records ----------------------------------------------------------------------------

REC = {"source_type": "records", "fetch": {"records": {
    "path": "$.lotteries", "id_field": "lottery_id", "fields": ["name", "borough", "status"],
}}}  # fmt: skip


def lotteries(*rows: tuple[int, str, str, str]) -> str:
    return json.dumps({"lotteries": [
        {"lottery_id": i, "name": n, "borough": b, "status": s, "views": i * 7}
        for i, n, b, s in rows
    ]}, indent=1)  # fmt: skip


L1 = (101, "Sunset Terrace", "BK", "Active")
L2 = (102, "Harbor View", "MN", "Active")
L3 = (103, "Grand Concourse Homes", "BX", "Closed")
JSON = "application/json"
case(
    "records-new-lottery", "json",
    [lotteries(L1, L2), lotteries(L1, L2, (104, "Riverside Commons", "QN", "Active"))],
    REC, [{"outcome": "alert", "added_words": 9}],
    "A lottery with an unseen ID appears.", JSON,
)  # fmt: skip
case(
    "records-status-becomes-active", "json",
    [lotteries(L1, L3), lotteries(L1, (103, "Grand Concourse Homes", "BX", "Active"))],
    REC, [{"outcome": "alert"}],
    "A watched field changes: Closed becomes Active.", JSON,
)  # fmt: skip
case(
    "records-unwatched-field-changes", "json",
    [lotteries(L1, L2), json.dumps({"lotteries": [
        {"lottery_id": 101, "name": "Sunset Terrace", "borough": "BK", "status": "Active",
         "views": 9999},
        {"lottery_id": 102, "name": "Harbor View", "borough": "MN", "status": "Active",
         "views": 1},
    ]})],
    REC, [{"outcome": "unchanged"}],
    "Only an unwatched field (views) changes: not a change.", JSON,
)  # fmt: skip
case(
    "records-reordered", "json",
    [lotteries(L1, L2, L3), lotteries(L3, L1, L2)],
    REC, [{"outcome": "unchanged"}],
    "The feed returns the same records in another order: records are sorted by ID.", JSON,
)  # fmt: skip
case(
    "records-removed-event-not-alerting", "json",
    [lotteries(L1, L2), lotteries(L1)],
    {**REC, "fetch": {"records": {**REC["fetch"]["records"], "events": ["new", "changed"]}}},
    [{"outcome": "suppressed", "reason": "records_events"}],
    "This bookmark alerts only on new and changed records; a removal is stored silently.", JSON,
)  # fmt: skip
case(
    "records-removed-event-alerting", "json",
    [lotteries(L1, L2), lotteries(L1)],
    REC, [{"outcome": "alert"}],
    "By default a record that disappears is an alert.", JSON,
)  # fmt: skip
case(
    "records-row-filter", "json",
    [lotteries(L1, L2, L3),
     lotteries(L1, L2, (103, "Grand Concourse Homes RENAMED", "BX", "Closed"))],
    {**REC, "fetch": {"records": {**REC["fetch"]["records"], "filter": "status = Active"}}},
    [{"outcome": "unchanged"}],
    "The row filter keeps only Active lotteries, so a change to a Closed one is invisible.", JSON,
)  # fmt: skip
case(
    "records-filter-by-borough-list", "json",
    [lotteries(L1, L2), lotteries(L1, L2, (104, "Riverside Commons", "QN", "Active"))],
    {**REC, "fetch": {"records": {**REC["fetch"]["records"], "filter": "borough in [MN, BK]"}}},
    [{"outcome": "unchanged"}],
    "A new lottery in a borough outside the filter is not seen.", JSON,
)  # fmt: skip
case(
    "records-csv-new-row", "csv",
    ["id,name,status\n1,Sunset,Active\n2,Harbor,Active\n",
     "id,name,status\n1,Sunset,Active\n2,Harbor,Active\n3,Riverside,Active\n"],
    {"source_type": "records", "fetch": {"records": {"id_field": "id"}}},
    [{"outcome": "alert"}], "A CSV dataset gains a row.", "text/csv",
)  # fmt: skip
print("written")
