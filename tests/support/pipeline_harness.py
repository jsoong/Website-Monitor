"""Drives ``process_check`` the way the runner does, keeping the version references."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pagewatch.engine.config import source_options
from pagewatch.engine.pipeline.core import PipelineJob, PipelineResult, VersionRef, process_check
from pagewatch.models import FetchConfig


class Harness:
    def __init__(self, root: Path, **cfg: Any) -> None:
        self.root = root
        self.cfg = cfg
        self.latest: VersionRef | None = None
        self.anchor: VersionRef | None = None
        self.versions: dict[int, VersionRef] = {}
        self.next_id = 1

    def run(
        self,
        body: str | bytes,
        *,
        ctype: str = "text/html",
        png: bytes | None = None,
        detect_js: bool = False,
        **over: Any,
    ) -> PipelineResult:
        data = body.encode() if isinstance(body, str) else body
        cfg = {**self.cfg, **over}
        job = PipelineJob(
            blob_root=str(self.root),
            body=data,
            content_type=ctype,
            final_url="https://example.com/",
            source_type=cfg.get("source_type", "auto"),
            filter_cfg=cfg.get("filter", {}),
            gate_cfg=cfg.get("gate", {}),
            highlight_mode=cfg.get("mode", cfg.get("highlight_mode", "standard")),
            latest=self.latest,
            anchor=self.anchor,
            source_cfg=source_options(FetchConfig.model_validate(cfg.get("fetch", {}))),
            screenshot_png=png,
            detect_js=detect_js,
        )
        res = process_check(job)
        if res.store_version:
            assert res.blocks_hash and res.filtered_hash
            ref = VersionRef(
                self.next_id, res.raw_hash, res.blocks_hash, res.filtered_hash, res.screenshot_hash
            )
            self.versions[ref.version_id] = ref
            self.next_id += 1
            self.latest = ref
            if res.kind == "first" or res.alert:
                self.anchor = ref
        return res
