"""Small behaviours the decision log promises: ntfy limits, what a program may be, and which
resources an exported page may pull in."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine.actions import program
from pagewatch.engine.actions.assets import localize_assets, stylesheet_links
from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.ntfy import MAX_MESSAGE_BYTES, build_payload
from pagewatch.models import NtfyParams
from tests.support.http_sink import HttpSink


def alert(**over: Any) -> AlertContext:
    fields: dict[str, Any] = {
        "bookmark_id": 1, "name": "Prix: café ☕", "url": "https://example.com/p?q=1",
        "info": (None, None, None), "change_id": 5, "detected_at": "2026-11-05T14:30:07.000000Z",
        "summary": "Nouveau", "added_words": 1, "removed_words": 0, "changed_blocks": 1,
        "checks_accumulated": 1, "keyword_hits": ["sale", "café"], "private": False,
    }  # fmt: skip
    return AlertContext(**{**fields, **over})


# -- ntfy ---------------------------------------------------------------------------------------


def test_ntfy_payload_for_a_normal_alert() -> None:
    body = build_payload(alert(), NtfyParams(priority="max", tags=["a"]), "t-1")
    assert body["topic"] == "t-1" and body["title"] == "Prix: café ☕"
    assert body["priority"] == 5 and body["tags"] == ["a"]
    assert body["click"] == "https://example.com/p?q=1"
    assert str(body["message"]).splitlines() == ["Nouveau", "Keywords: sale, café"]


def test_ntfy_message_is_cut_on_a_character_boundary_within_the_servers_limit() -> None:
    long = "é" * 5000  # two bytes each
    body = build_payload(alert(summary=long), NtfyParams(), "t-1")
    message = str(body["message"])
    assert len(message.encode("utf-8")) <= MAX_MESSAGE_BYTES
    assert message.endswith("...") and "\ufffd" not in message  # no half characters


def test_ntfy_private_payload_has_no_content_or_click() -> None:
    body = build_payload(alert(private=True), NtfyParams(), "t-1")
    assert body == {
        "topic": "t-1", "title": "Prix: café ☕", "message": "Changed. Open PageWatch to review.",
        "priority": 3,
    }  # fmt: skip


def test_ntfy_only_links_to_web_pages() -> None:
    assert "click" not in build_payload(alert(url="file:///C:/x.txt"), NtfyParams(), "t")
    assert "click" not in build_payload(alert(url=""), NtfyParams(), "t")


# -- run_program --------------------------------------------------------------------------------


def test_a_program_that_does_not_exist_is_a_configuration_error() -> None:
    with pytest.raises(ActionConfigError, match="was not found"):
        program.resolve_executable("definitely-not-a-program-xyz")
    with pytest.raises(ActionConfigError, match="was not found"):
        program.resolve_executable("/definitely/not/here")


def test_the_interpreter_resolves_by_absolute_path_or_name() -> None:
    assert program.resolve_executable(sys.executable) == sys.executable
    assert Path(program.resolve_executable(Path(sys.executable).name)).exists()


def test_batch_files_are_refused_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("run.bat", "RUN.CMD"):
        script = tmp_path / name
        script.write_text("@echo off")
        monkeypatch.setattr(program.sys, "platform", "win32")
        with pytest.raises(ActionConfigError, match="batch files are not supported"):
            program.resolve_executable(str(script))
    monkeypatch.undo()


@pytest.mark.parametrize(
    ("content_type", "url", "ext"),
    [
        ("text/html; charset=utf-8", "https://x/", ".html"),
        ("application/pdf", "https://x/a", ".pdf"),
        ("application/json", "https://x/a", ".json"),
        ("", "https://x/report.docx?v=2", ".docx"),
        ("application/octet-stream", "https://x/blob", ".bin"),
    ],
)
def test_extension_for(content_type: str, url: str, ext: str) -> None:
    assert program.extension_for(content_type, url) == ext


# -- html_assets --------------------------------------------------------------------------------

PAGE = (
    '<html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
    '<base href="https://example.com/"></head><body><img src="{img}" alt="x"></body></html>'
)


async def test_a_public_page_cannot_make_the_export_fetch_a_local_address(
    tmp_path: Path,
) -> None:
    sink = await HttpSink(assets={"/secret.png": (b"INTERNAL", "image/png")}).start()
    try:
        html, report = await localize_assets(
            PAGE.format(img=sink.url("/secret.png")),
            "https://example.com/page",
            tmp_path / "f",
            "f",
        )
    finally:
        await sink.stop()
    assert sink.requests == []  # nothing was requested
    assert report.saved == {} and any("local address" in s for s in report.skipped)
    assert "secret.png" not in html and 'src="data:,"' in html  # no remote reference remains
    assert not (tmp_path / "f").exists()


async def test_a_local_page_may_use_its_own_local_resources(tmp_path: Path) -> None:
    sink = await HttpSink(assets={"/logo.png": (b"PNGDATA", "image/png")}).start()
    try:
        html, report = await localize_assets(
            PAGE.format(img="/logo.png"), sink.url("/page"), tmp_path / "f", "f"
        )
    finally:
        await sink.stop()
    assert (tmp_path / "f" / "001-logo.png").read_bytes() == b"PNGDATA"
    assert 'src="f/001-logo.png"' in html
    assert "<base" not in html and "Content-Security-Policy" not in html  # now a local archive
    assert report.skipped == []


async def test_a_resource_that_cannot_be_fetched_is_skipped_and_reported(tmp_path: Path) -> None:
    sink = await HttpSink(status=404).start()
    try:
        html, report = await localize_assets(
            PAGE.format(img="/gone.png"), sink.url("/page"), tmp_path / "f", "f"
        )
    finally:
        await sink.stop()
    assert report.saved == {} and any("HTTP 404" in s for s in report.skipped)
    assert "gone.png" not in html


def test_stylesheet_links_reads_the_stored_pages_head() -> None:
    raw = (
        b'<html><head><link rel="stylesheet" href="/a.css"><link rel="icon" href="/i.ico">'
        b'<link rel="Stylesheet" href="https://cdn.example.com/b.css"><link rel="stylesheet" href="/a.css">'
        b'<link rel="stylesheet" href="javascript:alert(1)"></head><body></body></html>'
    )
    assert stylesheet_links(raw, "text/html", "https://example.com/dir/page") == [
        "https://example.com/a.css", "https://cdn.example.com/b.css",
    ]  # fmt: skip
    assert stylesheet_links(b'{"a": 1}', "application/json", "https://example.com/") == []
