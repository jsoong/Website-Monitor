"""Local files and folders: the mtime+size shortcut, listings, errors."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from pagewatch.engine.config import Resolved
from pagewatch.engine.fetch.base import FetchRequest
from pagewatch.engine.fetch.localfile import FileFetcher, content_type_for, url_to_path
from pagewatch.models import (
    ActionsConfig,
    FetchConfig,
    FilterConfig,
    GateConfig,
    ScheduleConfig,
    Settings,
)


def req(
    path: Path | str, *, etag: str | None = None, force: bool = False, **fetch: object
) -> FetchRequest:
    url = path if isinstance(path, str) else path.as_uri()
    return FetchRequest(
        url=url,
        resolved=Resolved(
            schedule=ScheduleConfig(), fetch=FetchConfig.model_validate(fetch), filter=FilterConfig(),
            gate=GateConfig(), actions=ActionsConfig(), overrides={},
        ),
        settings=Settings(),
        etag=etag,
        force=force,
    )  # fmt: skip


async def test_a_file_is_read_with_its_type_and_a_signature(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello world")
    res = await FileFetcher().fetch(req(f))
    assert res.error is None and res.body == b"hello world" and res.status == 200
    assert res.content_type == "text/plain" and not res.not_modified
    assert re.fullmatch(r"11-\d+", res.etag or "") and res.last_modified


async def test_unchanged_mtime_and_size_skip_reading_the_file(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("same")
    first = await FileFetcher().fetch(req(f))
    again = await FileFetcher().fetch(req(f, etag=first.etag))
    assert again.not_modified and again.body == b"" and again.etag == first.etag
    forced = await FileFetcher().fetch(req(f, etag=first.etag, force=True))
    assert not forced.not_modified and forced.body == b"same"


async def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one")
    first = await FileFetcher().fetch(req(f))
    f.write_text("two!")  # a different size, and a newer mtime
    res = await FileFetcher().fetch(req(f, etag=first.etag))
    assert not res.not_modified and res.body == b"two!" and res.etag != first.etag


async def test_same_size_new_mtime_is_read_again(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("aaaa")
    first = await FileFetcher().fetch(req(f))
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    res = await FileFetcher().fetch(req(f, etag=first.etag))
    assert not res.not_modified and res.body == b"aaaa"


@pytest.mark.parametrize(
    ("name", "ctype"),
    [
        ("a.pdf", "application/pdf"),
        ("a.DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("a.json", "application/json"),
        ("a.csv", "text/csv"),
        ("a.rss", "application/rss+xml"),
        ("a.html", "text/html"),
        ("a.png", "image/png"),
        ("a.unknownext", "application/octet-stream"),
    ],
)
def test_content_types_by_extension(name: str, ctype: str) -> None:
    assert content_type_for(name) == ctype


async def test_missing_and_too_large_files_are_errors(tmp_path: Path) -> None:
    res = await FileFetcher().fetch(req(tmp_path / "nope.txt"))
    assert res.error is not None and res.error.reason == "http_404" and not res.error.transient
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 5000)
    res = await FileFetcher().fetch(req(big, max_bytes=2048))
    assert res.error is not None and res.error.kind.value == "too_large"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a non-root POSIX user")
async def test_unreadable_file_is_http_403(tmp_path: Path) -> None:
    f = tmp_path / "secret.txt"
    f.write_text("x")
    f.chmod(0)
    res = await FileFetcher().fetch(req(f))
    assert res.error is not None and res.error.reason == "http_403"


async def test_a_folder_is_a_listing_table_of_name_size_and_modified(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("12345")
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("hidden unless recursive")
    res = await FileFetcher().fetch(req(tmp_path))
    html = res.body.decode()
    assert res.error is None and res.content_type.startswith("text/html")
    assert html.index("a.txt") < html.index("b.txt") < html.index("sub/")
    assert "<td>5</td>" in html and "deep.txt" not in html
    assert re.search(r"<td>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d</td>", html)
    assert res.etag is None  # a listing has no cheap signature: the hash decides


async def test_recursive_listing_includes_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "sub" / "deeper").mkdir(parents=True)
    (tmp_path / "sub" / "deeper" / "x.txt").write_text("x")
    (tmp_path / "top.txt").write_text("t")
    html = (await FileFetcher().fetch(req(tmp_path, listing={"recursive": True}))).body.decode()
    assert "sub/deeper/x.txt" in html and "sub/" in html and "top.txt" in html


async def test_a_listing_changes_when_a_file_is_added_or_resized(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("1")
    before = (await FileFetcher().fetch(req(tmp_path))).body
    (tmp_path / "new.txt").write_text("2")
    added = (await FileFetcher().fetch(req(tmp_path))).body
    (tmp_path / "a.txt").write_text("123456")
    resized = (await FileFetcher().fetch(req(tmp_path))).body
    assert before != added != resized and b"new.txt" in added


async def test_too_many_entries_is_an_error_not_a_silent_truncation(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}").write_text("x")
    res = await FileFetcher().fetch(req(tmp_path, listing={"max_entries": 3}))
    assert res.error is not None and res.error.kind.value == "too_large"
    assert "listing.max_entries" in res.error.message


async def test_listing_escapes_hostile_names(tmp_path: Path) -> None:
    (tmp_path / "<script>alert(1)<b>&.txt").write_text("x")
    html = (await FileFetcher().fetch(req(tmp_path))).body.decode()
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;b&gt;&amp;.txt" in html


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("file:///home/me/a%20b.txt", "/home/me/a b.txt"),
        ("file://localhost/tmp/x", "/tmp/x"),
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="POSIX path forms")
def test_url_to_path(url: str, expected: str) -> None:
    assert url_to_path(url) == Path(expected)
