"""One check, end to end: load -> fetch -> worker pipeline -> one atomic commit -> events.

The runner owns the check_run record, retry-once-on-transient-error, the error counter and
the schedule update. The fetched bytes go to the worker pool; everything that crosses back
is small. The commit (version, change, pointers, next due time, action jobs) is a single
database transaction.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from pagewatch.engine import schedule as sched
from pagewatch.engine.clock import iso
from pagewatch.engine.config import Resolved, resolve
from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.hostgate import host_of
from pagewatch.engine.logs import get_logger
from pagewatch.engine.pipeline.core import PipelineJob, PipelineResult, VersionRef, process_check
from pagewatch.engine.scheduler import RunResult
from pagewatch.engine.store import repo
from pagewatch.models import BookmarkStatus, CheckKind, FetchErrorKind, Outcome, Trigger

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.runner")


@dataclass(slots=True)
class _Context:
    row: sqlite3.Row
    latest: sqlite3.Row | None
    anchor: sqlite3.Row | None


def _ref(row: sqlite3.Row | None) -> VersionRef | None:
    if row is None:
        return None
    return VersionRef(row["id"], row["raw_hash"], row["blocks_hash"], row["filtered_hash"])


def _load_context(conn: sqlite3.Connection, bookmark_id: int) -> _Context | None:
    row = repo.bookmark_get(conn, bookmark_id)
    if row is None:
        return None
    latest = repo.version_get(conn, row["latest_version_id"])
    anchor = latest
    if row["gate_anchor_version_id"] != row["latest_version_id"]:
        anchor = repo.version_get(conn, row["gate_anchor_version_id"])
    return _Context(row, latest, anchor)


class CheckRunner:
    def __init__(self, engine: Engine, rng: random.Random | None = None) -> None:
        self.e = engine
        self.rng = rng or random.Random()

    # -- entry point --------------------------------------------------------------------

    async def run(self, bookmark_id: int, trigger: Trigger, force: bool) -> RunResult | None:
        e = self.e
        ctx = await e.db.read(lambda c: _load_context(c, bookmark_id))
        if ctx is None:
            return None  # deleted since it was queued
        row = ctx.row
        if not row["enabled"] and trigger is not Trigger.MANUAL:
            return None
        settings = e.settings
        now = e.clock.now()
        run_id = await e.db.write(
            lambda c: repo.check_run_start(
                c, bookmark_id, iso(now), trigger.value, CheckKind.STATIC.value
            )
        )
        e.events.publish("check_started", {"bookmark_id": bookmark_id, "trigger": trigger.value})
        try:
            resolved = resolve(row, e.folders, settings)
        except ValidationError as exc:
            return await self._fail(ctx, None, run_id, trigger, 0, FetchError(
                FetchErrorKind.PARSE, f"invalid configuration: {exc.errors()[0]['msg']}"
            ))  # fmt: skip

        fetch = await self._fetch(ctx, resolved, force)
        if fetch.error is not None:
            return await self._fail(ctx, resolved, run_id, trigger, fetch.elapsed_ms, fetch.error)

        if fetch.not_modified and ctx.latest is not None:
            return await self._commit_unchanged(ctx, resolved, run_id, fetch, "unchanged", 0)

        job = PipelineJob(
            blob_root=str(e.data_dir.blobs_dir),
            body=fetch.body,
            content_type=fetch.content_type,
            final_url=fetch.final_url,
            source_type=row["source_type"],
            filter_cfg=resolved.filter.model_dump(mode="json"),
            gate_cfg=resolved.gate.model_dump(mode="json"),
            highlight_mode=row["highlight_mode"],
            latest=_ref(ctx.latest),
            anchor=_ref(ctx.anchor),
        )
        try:
            res = await e.pool.run(process_check, job)
        except Exception as exc:
            log.exception("pipeline_failed", bookmark_id=bookmark_id)
            return await self._fail(ctx, resolved, run_id, trigger, fetch.elapsed_ms, FetchError(
                FetchErrorKind.PARSE, f"processing failed: {type(exc).__name__}: {exc}"[:300]
            ))  # fmt: skip
        return await self._commit_result(ctx, resolved, run_id, fetch, res)

    # -- fetch --------------------------------------------------------------------------

    async def _fetch(self, ctx: _Context, resolved: Resolved, force: bool) -> FetchResult:
        url: str = ctx.row["url"]
        if not url.lower().startswith(("http://", "https://")):
            return FetchResult(
                url, None, {}, "", b"",
                error=FetchError(FetchErrorKind.PARSE, f"unsupported url scheme: {url[:40]}"),
            )  # fmt: skip
        req = FetchRequest(
            url=url,
            resolved=resolved,
            settings=self.e.settings,
            etag=ctx.latest["etag"] if ctx.latest else None,
            last_modified=ctx.latest["last_modified"] if ctx.latest else None,
            force=force,
        )
        return await self.e.static_fetcher.fetch(req)

    # -- scheduling ---------------------------------------------------------------------

    def _next(
        self, ctx: _Context, resolved: Resolved | None, *, changed: bool | None
    ) -> tuple[Any, int | None]:
        e = self.e
        cfg = resolved.schedule if resolved else e.settings.default_schedule
        return sched.next_due(
            cfg,
            e.clock.now(),
            current_interval_s=ctx.row["current_interval_s"],
            changed=changed,
            on_battery=e.on_battery,
            zone=e.zone,
            rng=self.rng,
        )

    # -- commits ------------------------------------------------------------------------

    async def _commit_unchanged(
        self,
        ctx: _Context,
        resolved: Resolved,
        run_id: int,
        fetch: FetchResult,
        reason_kind: str,
        elapsed_ms: int,
    ) -> RunResult:
        due, interval = self._next(ctx, resolved, changed=False)
        refresh = ctx.latest is not None and (
            (fetch.etag and fetch.etag != ctx.latest["etag"])
            or (fetch.last_modified and fetch.last_modified != ctx.latest["last_modified"])
        )
        commit = repo.CheckCommit(
            bookmark_id=ctx.row["id"],
            run_id=run_id,
            finished_at=iso(self.e.clock.now()),
            outcome=Outcome.UNCHANGED,
            reason=None,
            duration_ms=fetch.elapsed_ms + elapsed_ms,
            byte_count=len(fetch.body) or None,
            next_due_at=iso(due) if due else None,
            current_interval_s=interval,
            refresh_etag_of=ctx.latest["id"] if refresh and ctx.latest else None,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
        )
        await self._write(ctx, commit)
        return RunResult(due)

    async def _commit_result(
        self,
        ctx: _Context,
        resolved: Resolved,
        run_id: int,
        fetch: FetchResult,
        res: PipelineResult,
    ) -> RunResult:
        e = self.e
        row = ctx.row
        if res.kind in ("unchanged_raw", "unchanged"):
            return await self._commit_unchanged(
                ctx, resolved, run_id, fetch, res.kind, res.elapsed_ms
            )

        now = e.clock.now()
        base: dict[str, Any] = {
            "bookmark_id": row["id"],
            "run_id": run_id,
            "finished_at": iso(now),
            "duration_ms": fetch.elapsed_ms + res.elapsed_ms,
            "byte_count": len(fetch.body),
        }
        if res.kind == "rejected":
            due, interval = self._next(ctx, resolved, changed=None)
            commit = repo.CheckCommit(
                **base,
                outcome=Outcome.SUPPRESSED,
                reason=res.reason,
                next_due_at=iso(due) if due else None,
                current_interval_s=interval,
            )
            await self._write(ctx, commit)
            return RunResult(due)

        assert res.blocks_hash and res.filtered_hash
        new_version = repo.NewVersion(
            fetched_at=iso(now),
            raw_hash=res.raw_hash,
            blocks_hash=res.blocks_hash,
            filtered_hash=res.filtered_hash,
            http_status=fetch.status,
            content_type=fetch.content_type or None,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            byte_size=len(fetch.body),
            word_count=res.word_count,
        )
        change: repo.NewChange | None = None
        outcome = Outcome.SUPPRESSED
        reason = res.reason
        is_first = res.kind == "first"
        if is_first:
            outcome, reason = Outcome.FIRST, None
            if e.settings.notify_on_first_check:
                change = repo.NewChange(
                    old_version_id=None,
                    detected_at=iso(now),
                    added_words=res.word_count,
                    removed_words=0,
                    changed_blocks=None,
                    anchor_based=False,
                    summary=res.summary,
                )
        elif res.alert:
            outcome, reason = Outcome.CHANGED, None
            stats = res.stats or {}
            change = repo.NewChange(
                old_version_id=res.compared_with,
                detected_at=iso(now),
                added_words=stats.get("added_words"),
                removed_words=stats.get("removed_words"),
                changed_blocks=stats.get("changed_blocks"),
                anchor_based=res.anchor_based,
                keyword_hits=res.keyword_hits,
                diff_hash=res.diff_hash,
                summary=res.summary,
            )
        due, interval = self._next(ctx, resolved, changed=True)
        commit = repo.CheckCommit(
            **base,
            outcome=outcome,
            reason=reason,
            next_due_at=iso(due) if due else None,
            current_interval_s=interval,
            new_version=new_version,
            is_first=is_first,
            change=change,
            action_types=[a.type.value for a in resolved.actions.actions] if change else [],
        )
        await self._write(ctx, commit)
        return RunResult(due)

    async def _fail(
        self,
        ctx: _Context,
        resolved: Resolved | None,
        run_id: int,
        trigger: Trigger,
        elapsed_ms: int,
        err: FetchError,
    ) -> RunResult:
        e = self.e
        row = ctx.row
        host = host_of(row["url"])
        if err.retry_after_s:
            e.host_gate.backoff(host, err.retry_after_s)
        threshold = resolved.gate.error_threshold if resolved else 3
        errors = int(row["consecutive_errors"])
        status: BookmarkStatus | None = None
        next_trigger = Trigger.SCHEDULE
        counted = e.online
        if err.transient and e.online and trigger is not Trigger.RETRY:
            counted = False  # one quick retry before it counts
            due = e.clock.now() + timedelta(seconds=e.settings.transient_retry_s)
            interval = row["current_interval_s"]
            next_trigger = Trigger.RETRY
        else:
            due, interval = self._next(ctx, resolved, changed=None)
        became_error = False
        if counted:
            errors += 1
            if errors >= threshold:
                status = BookmarkStatus.ERROR
                became_error = int(row["consecutive_errors"]) < threshold
        commit = repo.CheckCommit(
            bookmark_id=row["id"],
            run_id=run_id,
            finished_at=iso(e.clock.now()),
            outcome=Outcome.ERROR,
            reason=err.reason,
            duration_ms=elapsed_ms,
            byte_count=None,
            next_due_at=iso(due) if due else None,
            current_interval_s=interval,
            consecutive_errors=errors if counted else int(row["consecutive_errors"]),
            status=status,
        )
        await self._write(ctx, commit, error=err)
        if became_error:
            e.notify_problem(
                row["id"], f"{row['name']} is failing", f"{err.reason}: {err.message}"[:200]
            )
        return RunResult(due, next_trigger)

    async def _write(
        self, ctx: _Context, commit: repo.CheckCommit, error: FetchError | None = None
    ) -> repo.CommitResult:
        e = self.e
        result = await e.db.write(lambda c: repo.commit_check(c, commit))
        row = ctx.row
        log.info(
            "check",
            bookmark_id=row["id"],
            method=CheckKind.STATIC.value,
            outcome=commit.outcome.value,
            reason=commit.reason,
            duration_ms=commit.duration_ms,
        )
        e.events.publish(
            "check_finished",
            {
                "bookmark_id": row["id"],
                "outcome": commit.outcome.value,
                "reason": commit.reason,
                "duration_ms": commit.duration_ms,
            },
        )
        if result.change_id is not None:
            e.events.publish(
                "change_detected", {"bookmark_id": row["id"], "change_id": result.change_id}
            )
        if result.version_id is not None or commit.outcome is Outcome.ERROR:
            e.events.publish("bookmark_updated", {"bookmark_id": row["id"]})
        if result.job_ids:
            e.actions.kick()
        return result
