"""Plugin hooks through a real worker *process*: the spec pickles, the plugin file is imported by the
child, and state, notes and alerts come back in the result."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from textwrap import dedent

import pytest

from pagewatch.engine.pipeline.core import PipelineJob, VersionRef, process_check
from pagewatch.engine.workers import WorkerPool

PAGE_1 = b"<html><body><p>alpha</p><p>beta</p></body></html>"
PAGE_2 = b"<html><body><p>alpha</p><p>beta</p><p>URGENT gamma</p></body></html>"

PLUGIN = '''
"""Exercises every hook that runs in a worker."""
from pagewatch.plugin_api import Verdict

def preprocess(ctx, html):
    ctx.state["pre"] = ctx.state.get("pre", 0) + 1
    return html.replace("alpha", "ALPHA")

def check_keywords(ctx, added_text):
    ctx.notify("kw", added_text)
    return "urgent" in added_text.lower()

def compare(ctx, old, new):
    ctx.set_property("info3", f"{len(old)}->{len(new)}")
    ctx.state["diff"] = ctx.diff
    return Verdict.change("because")
'''


@pytest.fixture
async def pool() -> AsyncIterator[WorkerPool]:
    p = WorkerPool(1, mode="process")
    yield p
    p.shutdown()


def spec(path: Path, state: dict[str, object] | None = None) -> dict[str, object]:
    bookmark = {
        "id": 1, "name": "n", "url": "https://x.test/", "folder_id": None, "source_type": "auto",
        "check_method": "auto", "info1": None, "info2": None, "info3": None, "note": None,
        "plugin": "wk", "status": "ok",
    }  # fmt: skip
    return {"name": "wk", "path": str(path), "bookmark": bookmark, "state": state or {},
            "now": "2026-01-05T12:00:00.000000Z", "user_agent": ""}  # fmt: skip


def job(tmp_path: Path, body: bytes, plugin: dict[str, object], latest: VersionRef | None = None,
        **gate: object) -> PipelineJob:  # fmt: skip
    return PipelineJob(
        blob_root=str(tmp_path / "blobs"), body=body, content_type="text/html",
        final_url="https://x.test/", source_type="auto", filter_cfg={}, gate_cfg=dict(gate),
        highlight_mode="standard", latest=latest, anchor=latest, source_cfg={"plugin": plugin},
    )  # fmt: skip


async def test_hooks_run_in_the_child_and_their_effects_come_back(
    pool: WorkerPool, tmp_path: Path
) -> None:
    path = tmp_path / "wk.py"
    path.write_text(dedent(PLUGIN), encoding="utf-8")
    first = await pool.run(process_check, job(tmp_path, PAGE_1, spec(path)))
    assert first.kind == "first" and first.plugin is not None
    assert first.plugin.state == {"pre": 1}  # preprocess ran in the child; its state came back
    ref = VersionRef(1, first.raw_hash, first.blocks_hash or "", first.filtered_hash or "")

    second = await pool.run(
        process_check,
        job(tmp_path, PAGE_2, spec(path, {"pre": 1}), ref, keywords="never-matches"),
    )
    assert second.kind == "stored" and second.alert and second.reason is None  # compare said so
    assert second.keyword_hits == ["plugin:wk"]  # check_keywords: True, OR-ed with the rules
    out = second.plugin
    assert out is not None and out.notes == []
    assert out.state["pre"] == 2
    assert out.state["diff"]["added_words"] == 2  # ctx.diff carried the statistics
    assert out.props == {"info3": "2->3"}  # the blocks, not the HTML
    assert out.alerts == [("kw", "URGENT gamma")]


async def test_a_plugin_that_raises_in_the_child_is_reported_not_fatal(
    pool: WorkerPool, tmp_path: Path
) -> None:
    path = tmp_path / "wk.py"
    path.write_text("def preprocess(ctx, html): raise RuntimeError('nope')\n", encoding="utf-8")
    res = await pool.run(process_check, job(tmp_path, PAGE_1, spec(path)))
    assert res.kind == "first" and res.plugin is not None
    assert res.plugin.notes == ["plugin_error:preprocess"]
    assert any("preprocess raised RuntimeError: nope" in text for _, text in res.plugin.logs)
