"""The M4 jobs through a real worker *process*: the new job fields (documents, a PNG, the records
layout) pickle, results come back small, and expected failures keep their type across the boundary
(the runner names them by type)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pagewatch.engine.pipeline.core import (
    PipelineJob,
    RenderJob,
    ViewVersion,
    process_check,
    render_view,
)
from pagewatch.engine.workers import WorkerPool
from tests.support.docs import make_pdf, page_png


def job(tmp_path: Path, body: bytes, **kw: object) -> PipelineJob:
    base: dict[str, object] = {
        "blob_root": str(tmp_path / "blobs"), "body": body, "content_type": "application/pdf",
        "final_url": "https://x.test/a.pdf", "source_type": "auto", "filter_cfg": {},
        "gate_cfg": {}, "highlight_mode": "standard", "latest": None, "anchor": None,
    }  # fmt: skip
    return PipelineJob(**{**base, **kw})  # type: ignore[arg-type]


@pytest.fixture
async def pool() -> WorkerPool:
    p = WorkerPool(1, mode="process")
    yield p  # type: ignore[misc]
    p.shutdown()


async def test_a_pdf_and_a_screenshot_and_records_run_in_a_worker_process(
    pool: WorkerPool, tmp_path: Path
) -> None:
    res = await pool.run(process_check, job(tmp_path, make_pdf([["hello from a worker"]])))
    assert res.kind == "first" and res.source_kind == "pdf" and res.blocks_hash

    png = page_png([(10, 10, 100, 40, "black")])
    shot = await pool.run(
        process_check,
        job(
            tmp_path,
            b"<html><body>hi there</body></html>",
            content_type="text/html",
            screenshot_png=png,
        ),  # fmt: skip
    )
    assert shot.kind == "first" and shot.screenshot_hash and shot.source_kind == "html"

    records = await pool.run(
        process_check,
        job(
            tmp_path,
            b'[{"id": 1, "n": "a"}]',
            content_type="application/json",
            source_type="records",
            source_cfg={"records": {"id_field": "id"}},
        ),  # fmt: skip
    )
    assert records.kind == "first" and records.source_kind == "records"

    rendered = await pool.run(
        render_view,
        RenderJob(
            blob_root=str(tmp_path / "blobs"), view="screenshot", url="https://x.test/",
            new=ViewVersion(None, "", shot.blocks_hash or "", shot.screenshot_hash), old=None,
        ),
    )  # fmt: skip
    assert rendered.view == "screenshot" and rendered.png == png  # an unchanged picture comes back


async def test_expected_failures_keep_their_type_across_the_process_boundary(
    pool: WorkerPool, tmp_path: Path
) -> None:
    with pytest.raises(ValueError) as pdf:
        await pool.run(process_check, job(tmp_path, b"%PDF-1.4 broken"))
    assert type(pdf.value).__name__ == "DocumentError" and "unreadable PDF" in str(pdf.value)
    with pytest.raises(ValueError) as rec:
        await pool.run(
            process_check,
            job(tmp_path, b'{"other": 1}', content_type="application/json",
                source_type="records", source_cfg={"records": {"id_field": "id", "path": "$.x"}}),
        )  # fmt: skip
    assert type(rec.value).__name__ == "RecordsError" and "matched nothing" in str(rec.value)
