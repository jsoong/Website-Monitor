"""FTP against an in-process aioftp server: files, listings, login, not-modified, errors."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pagewatch.engine.config import Resolved
from pagewatch.engine.fetch.base import FetchRequest
from pagewatch.engine.fetch.ftp import FtpFetcher
from pagewatch.engine.secrets import MemorySecrets
from pagewatch.models import (
    ActionsConfig,
    FetchConfig,
    FilterConfig,
    GateConfig,
    ScheduleConfig,
    Settings,
)
from tests.support.docs import make_pdf
from tests.support.ftp_server import Ftp, serve


@pytest.fixture
async def ftp(tmp_path: Path) -> AsyncIterator[Ftp]:
    async with serve(tmp_path / "srv") as server:
        yield server


def req(url: str, *, etag: str | None = None, force: bool = False, **fetch: object) -> FetchRequest:
    fetch.setdefault("timeout_s", 5)
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


async def test_anonymous_download_of_a_text_file(ftp: Ftp) -> None:
    (ftp.root / "notice.txt").write_text("opening hours: 9 to 5")
    res = await FtpFetcher().fetch(req(ftp.url("/notice.txt")))
    assert res.error is None and res.body == b"opening hours: 9 to 5"
    assert res.content_type == "text/plain" and res.status == 200 and res.etag


async def test_a_pdf_keeps_its_document_type(ftp: Ftp) -> None:
    (ftp.root / "report.pdf").write_bytes(make_pdf([["hello"]]))
    res = await FtpFetcher().fetch(req(ftp.url("/report.pdf")))
    assert res.error is None and res.content_type == "application/pdf" and res.body[:5] == b"%PDF-"


async def test_unchanged_size_and_modified_time_skip_the_download(ftp: Ftp) -> None:
    f = ftp.root / "a.txt"
    f.write_text("one")
    first = await FtpFetcher().fetch(req(ftp.url("/a.txt")))
    again = await FtpFetcher().fetch(req(ftp.url("/a.txt"), etag=first.etag))
    assert again.not_modified and again.body == b""
    forced = await FtpFetcher().fetch(req(ftp.url("/a.txt"), etag=first.etag, force=True))
    assert not forced.not_modified and forced.body == b"one"
    f.write_text("longer now")
    changed = await FtpFetcher().fetch(req(ftp.url("/a.txt"), etag=first.etag))
    assert not changed.not_modified and changed.body == b"longer now"


async def test_a_directory_is_a_listing_table(ftp: Ftp) -> None:
    (ftp.root / "pub").mkdir()
    (ftp.root / "pub" / "b.txt").write_text("12345")
    (ftp.root / "pub" / "a.txt").write_text("1")
    (ftp.root / "pub" / "sub").mkdir()
    (ftp.root / "pub" / "sub" / "deep.txt").write_text("deep")
    res = await FtpFetcher().fetch(req(ftp.url("/pub")))
    html = res.body.decode()
    assert res.error is None and res.content_type.startswith("text/html")
    assert html.index("a.txt") < html.index("b.txt") < html.index("sub/")
    assert "<td>5</td>" in html and "deep.txt" not in html
    rec = (
        await FtpFetcher().fetch(req(ftp.url("/pub"), listing={"recursive": True}))
    ).body.decode()
    assert "sub/deep.txt" in rec


async def test_listing_changes_with_the_directory(ftp: Ftp) -> None:
    (ftp.root / "a.txt").write_text("1")
    before = (await FtpFetcher().fetch(req(ftp.url("/")))).body
    (ftp.root / "b.txt").write_text("2")
    assert (await FtpFetcher().fetch(req(ftp.url("/")))).body != before


async def test_login_with_a_stored_secret(ftp: Ftp) -> None:
    (ftp.root / "a.txt").write_text("private")
    fetcher = FtpFetcher(MemorySecrets({"ftp-alice": "s3cret"}))
    auth = {"username": "alice", "secret_key": "ftp-alice"}
    res = await fetcher.fetch(req(ftp.url("/a.txt"), auth=auth))
    assert res.error is None and res.body == b"private"


async def test_a_wrong_password_is_http_401_and_a_missing_secret_is_named(ftp: Ftp) -> None:
    auth = {"username": "alice", "secret_key": "k"}
    bad = await FtpFetcher(MemorySecrets({"k": "wrong"})).fetch(req(ftp.url("/a.txt"), auth=auth))
    assert bad.error is not None and bad.error.reason == "http_401"
    none = await FtpFetcher().fetch(req(ftp.url("/a.txt"), auth=auth))
    assert none.error is not None and none.error.reason == "http_401"
    assert "no stored secret named 'k'" in none.error.message


async def test_a_missing_path_is_http_404(ftp: Ftp) -> None:
    res = await FtpFetcher().fetch(req(ftp.url("/nothing.txt")))
    assert res.error is not None and res.error.reason == "http_404" and not res.error.transient


async def test_too_large_by_declared_size_and_by_entries(ftp: Ftp) -> None:
    (ftp.root / "big.bin").write_bytes(b"x" * 5000)
    res = await FtpFetcher().fetch(req(ftp.url("/big.bin"), max_bytes=2048))
    assert res.error is not None and res.error.kind.value == "too_large"
    for i in range(5):
        (ftp.root / f"f{i}").write_text("x")
    res = await FtpFetcher().fetch(req(ftp.url("/"), listing={"max_entries": 3}))
    assert res.error is not None and res.error.kind.value == "too_large"


async def test_a_refused_connection_is_a_transient_connection_error() -> None:
    res = await FtpFetcher().fetch(req("ftp://127.0.0.1:9/x", timeout_s=2))
    assert res.error is not None and res.error.kind.value == "connection" and res.error.transient


async def test_a_url_without_a_host_is_a_parse_error() -> None:
    res = await FtpFetcher().fetch(req("ftp:///nohost"))
    assert res.error is not None and res.error.kind.value == "parse"


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX path")
async def test_percent_encoded_paths(ftp: Ftp) -> None:
    (ftp.root / "my file.txt").write_text("spaced")
    res = await FtpFetcher().fetch(req(ftp.url("/my%20file.txt")))
    assert res.error is None and res.body == b"spaced"
