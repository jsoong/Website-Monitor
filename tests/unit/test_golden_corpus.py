"""Golden corpus: page sets with an expected outcome per step and a snapshot of the
highlighted (text) diff of every alert.

Layout: tests/fixtures/sites/<case>/{case.json, v1.<ext>, v2.<ext>, ...} plus expected.txt.
Step 1 is the baseline ("first"); case.json's ``expect`` lists one expectation per later step.
Regenerate snapshots with ``UPDATE_GOLDEN=1 pytest tests/unit/test_golden_corpus.py`` and
*review the diff*: a changed snapshot is a changed user-visible behaviour.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from pagewatch.engine.pipeline.core import PipelineResult
from pagewatch.engine.pipeline.diff import DiffResult
from pagewatch.engine.pipeline.extract import blocks_from_json
from pagewatch.engine.pipeline.render import render_marks
from pagewatch.engine.store.blobs import BlobStore
from tests.support.pipeline_harness import Harness

SITES = Path(__file__).resolve().parents[1] / "fixtures" / "sites"
CASES = sorted(p for p in SITES.iterdir() if (p / "case.json").exists())
UPDATE = bool(os.environ.get("UPDATE_GOLDEN"))


def outcome_of(res: PipelineResult) -> str:
    if res.kind in ("unchanged", "unchanged_raw"):
        return "unchanged"
    if res.kind == "stored":
        return "alert" if res.alert else "suppressed"
    return res.kind  # first | rejected


def test_the_corpus_is_big_enough() -> None:
    assert len(CASES) >= 30


@pytest.mark.parametrize("case_dir", CASES, ids=lambda p: p.name)
def test_golden_case(case_dir: Path, tmp_path: Path) -> None:
    meta: dict[str, Any] = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    ext = meta.get("ext", "html")
    files = sorted(
        case_dir.glob(f"v*.{ext}"), key=lambda p: int(re.search(r"v(\d+)", p.stem).group(1))
    )  # type: ignore[union-attr]
    assert len(files) == len(meta["expect"]) + 1, "one expectation per step after the baseline"
    h = Harness(tmp_path / "blobs", **meta["bookmark"])
    ctype = meta.get("content_type", "text/html")
    store = BlobStore(tmp_path / "blobs")

    results = [h.run(f.read_bytes(), ctype=ctype) for f in files]
    assert results[0].kind == "first", "step 1 must store the baseline"
    snapshot: list[str] = []
    for step, (res, exp) in enumerate(zip(results[1:], meta["expect"], strict=True), start=2):
        label = f"{case_dir.name} step {step}"
        assert outcome_of(res) == exp["outcome"], f"{label}: {res.kind} reason={res.reason}"
        if "reason" in exp:
            assert res.reason == exp["reason"], label
        if "keyword_hits" in exp:
            assert res.keyword_hits == exp["keyword_hits"], label
        if "added_words" in exp:
            assert res.stats and res.stats["added_words"] == exp["added_words"], (
                f"{label}: {res.stats}"
            )
        if "removed_words" in exp:
            assert res.stats and res.stats["removed_words"] == exp["removed_words"], (
                f"{label}: {res.stats}"
            )
        if res.kind == "stored" and res.alert:
            assert res.blocks_hash and res.diff_hash and res.compared_with is not None
            old = [
                b.text
                for b in blocks_from_json(store.get_json(h.versions[res.compared_with].blocks_hash))
            ]
            new = [b.text for b in blocks_from_json(store.get_json(res.blocks_hash))]
            diff = DiffResult.from_json(store.get_json(res.diff_hash))
            snapshot += [f"# step {step}", *render_marks(diff, old, new), ""]

    expected_file = case_dir / "expected.txt"
    text = "\n".join(snapshot)
    if UPDATE:
        if snapshot:
            expected_file.write_text(text, encoding="utf-8")
        elif expected_file.exists():
            expected_file.unlink()
    elif snapshot or expected_file.exists():
        assert expected_file.exists(), (
            f"missing snapshot (UPDATE_GOLDEN=1 to create): {expected_file}"
        )
        assert text == expected_file.read_text(encoding="utf-8")
