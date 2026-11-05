"""One check, end to end: load -> fetch -> worker pipeline -> one atomic commit -> events.

The runner owns the check_run record, retry-once-on-transient-error, the error counter and
the schedule update. The fetched bytes go to the worker pool; everything that crosses back
is small. The commit (version, change, pointers, next due time, action jobs) is a single
database transaction.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from pydantic import ValidationError

from pagewatch.engine import schedule as sched
from pagewatch.engine.clock import iso
from pagewatch.engine.config import Resolved, resolve
from pagewatch.engine.fetch.base import FetchError, FetchRequest, FetchResult
from pagewatch.engine.fetch.select import BROWSER, Route, method_kind, route_for
from pagewatch.engine.hostgate import host_of
from pagewatch.engine.logs import get_logger
from pagewatch.engine.pipeline.core import PipelineJob, PipelineResult, VersionRef, process_check
from pagewatch.engine.pipeline.merge import merge_documents, next_link
from pagewatch.engine.pipeline.sources import resolve_kind
from pagewatch.engine.plugins import EngineSession
from pagewatch.engine.scheduler import RunResult
from pagewatch.engine.store import repo
from pagewatch.engine.store.blobs import sha256_hex
from pagewatch.engine.urls import normalize_url
from pagewatch.engine.workers import WorkerTimeout
from pagewatch.models import BookmarkStatus, CheckKind, FetchErrorKind, Outcome, Trigger, loads
from pagewatch.plugin_api import ChangeInfo, CheckRequest, FetchedPage, RunInfo

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.runner")

# Failures that may just mean "this machine has no network": ask before counting them.
NETWORK_KINDS = (FetchErrorKind.DNS, FetchErrorKind.CONNECTION, FetchErrorKind.TIMEOUT)


@dataclass(slots=True)
class _Context:
    row: sqlite3.Row
    latest: sqlite3.Row | None
    anchor: sqlite3.Row | None
    plugin: EngineSession | None = None  # the bookmark's plugin for this check, if it has one


def _htmlish(content_type: str, body: bytes) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    return "html" in ct or (not ct and b"<" in body[:2048])


def _ref(row: sqlite3.Row | None) -> VersionRef | None:
    if row is None:
        return None
    return VersionRef(
        row["id"], row["raw_hash"], row["blocks_hash"], row["filtered_hash"], row["screenshot_hash"]
    )


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
        first_guess = route_for(row["url"], row["source_type"], row["check_method"])
        run_id = await e.db.write(
            lambda c: repo.check_run_start(
                c, bookmark_id, iso(now), trigger.value,
                (first_guess.kind if first_guess else CheckKind.STATIC).value,
            )
        )  # fmt: skip
        e.events.publish("check_started", {"bookmark_id": bookmark_id, "trigger": trigger.value})
        try:
            resolved = resolve(row, e.folders, settings)
        except ValidationError as exc:
            return await self._fail(ctx, None, run_id, trigger, 0, FetchError(
                FetchErrorKind.PARSE, f"invalid configuration: {exc.errors()[0]['msg']}"
            ))  # fmt: skip
        if row["source_type"] == "records" and resolved.fetch.records is None:
            return await self._fail(ctx, resolved, run_id, trigger, 0, FetchError(
                FetchErrorKind.PARSE, "a records source needs a 'records' configuration"
            ))  # fmt: skip
        url: str = row["url"]
        if row["plugin"]:
            ctx.plugin = await e.plugins.open(row)
            if ctx.plugin is not None and ctx.plugin.active:
                request = CheckRequest(url, dict(resolved.fetch.headers), resolved.fetch.body)
                if not await ctx.plugin.before_check(request):
                    return await self._commit_skipped(ctx, resolved, run_id)
                resolved, url = self._apply_request(resolved, request, url)
        route = route_for(url, row["source_type"], row["check_method"], resolved.fetch)
        if route is None:
            return await self._fail(ctx, resolved, run_id, trigger, 0, FetchError(
                FetchErrorKind.PARSE, f"unsupported url scheme: {url[:40]}"
            ))  # fmt: skip

        fetch = await self._fetch(ctx, resolved, force, route, url)
        if fetch.error is not None:
            return await self._fail(
                ctx, resolved, run_id, trigger, fetch.elapsed_ms, fetch.error, route
            )
        if fetch.not_modified and ctx.latest is not None:
            kind = resolve_kind(
                row["source_type"], ctx.latest["content_type"] or "", row["url"], b""
            )
            return await self._commit_unchanged(
                ctx, resolved, run_id, fetch, 0, method_kind(route, kind)
            )

        # Method auto-detection (spec): the first check of an `auto` web bookmark runs static;
        # if that shows (almost) nothing, it is re-run in the browser and the choice is kept.
        detect = (
            row["check_method"] == "auto"
            and ctx.latest is None
            and route.name == "static"
            and row["source_type"] in ("auto", "html")
        )
        switched: str | None = None
        while True:
            source_cfg = e.source_cfg(row, resolved)
            if ctx.plugin is not None:  # the live state, and hooks that run in the worker
                spec = ctx.plugin.spec_for_worker()
                if spec is not None:
                    source_cfg["plugin"] = spec
                else:
                    source_cfg.pop("plugin", None)
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
                source_cfg=source_cfg,
                screenshot_png=fetch.screenshot_png,
                detect_js=detect,
            )
            try:
                res = await e.pool.run(process_check, job)
            except Exception as exc:
                return await self._fail(
                    ctx, resolved, run_id, trigger, fetch.elapsed_ms, self._processing_error(
                        bookmark_id, exc
                    ), route,
                )  # fmt: skip
            if ctx.plugin is not None:
                await ctx.plugin.after_pipeline(res.plugin)
            if not res.needs_browser:
                break
            switched, detect, route = res.reason or "app_shell", False, BROWSER
            log.info("method_switched", bookmark_id=bookmark_id, to="browser", reason=switched)
            static_ms = fetch.elapsed_ms
            fetch = await self._fetch(ctx, resolved, True, route, url)
            fetch.elapsed_ms += static_ms
            if fetch.error is not None:  # it needs a browser and none works: say so, loudly
                return await self._fail(
                    ctx, resolved, run_id, trigger, fetch.elapsed_ms, fetch.error, route
                )
        out = await self._commit_result(ctx, resolved, run_id, fetch, res, route, switched)
        if res.kind in ("first", "stored") and route.name in ("static", "browser"):
            # a new version of a page: Follow-Links may find new links on it
            e.follow.after_check(bookmark_id, fetch.body, fetch.content_type, fetch.final_url)
        return out

    @staticmethod
    def _processing_error(bookmark_id: int, exc: Exception) -> FetchError:
        if isinstance(exc, WorkerTimeout):
            log.warning("pipeline_timeout", bookmark_id=bookmark_id, error=str(exc))
            return FetchError(FetchErrorKind.PARSE, f"processing timed out: {exc}"[:300])
        # An unreadable document or a feed whose layout changed is an expected failure of the
        # check (named plainly); anything else is a bug and gets its stack trace in the log.
        if isinstance(exc, ValueError) and type(exc).__name__ in (
            "DocumentError", "RecordsError", "RecordsSyntaxError",
        ):  # fmt: skip
            log.warning("source_unreadable", bookmark_id=bookmark_id, error=str(exc)[:200])
            return FetchError(FetchErrorKind.PARSE, str(exc)[:300])
        log.exception("pipeline_failed", bookmark_id=bookmark_id)
        return FetchError(
            FetchErrorKind.PARSE, f"processing failed: {type(exc).__name__}: {exc}"[:300]
        )

    # -- fetch --------------------------------------------------------------------------

    @staticmethod
    def _apply_request(resolved: Resolved, request: CheckRequest, url: str) -> tuple[Resolved, str]:
        """The check with whatever ``before_check`` changed in the request."""
        fetch = resolved.fetch
        if request.headers == fetch.headers and request.body == fetch.body:
            return resolved, request.url or url
        update: dict[str, Any] = {"headers": dict(request.headers), "body": request.body}
        if request.body is not None:
            update["method"] = "POST"
        return replace(resolved, fetch=fetch.model_copy(update=update)), request.url or url

    def _merging(self, ctx: _Context, resolved: Resolved) -> bool:
        cfg = resolved.fetch.merge
        plugin = ctx.plugin.session if ctx.plugin is not None else None
        return bool(cfg and cfg.enabled) or bool(plugin and plugin.has("merge_pages"))

    async def _fetch(
        self,
        ctx: _Context,
        resolved: Resolved,
        force: bool,
        route: Route,
        url: str | None = None,
    ) -> FetchResult:
        url = url or ctx.row["url"]
        merging = self._merging(ctx, resolved)
        # With several pages one page's ETag says nothing about the others: always fetch them.
        use_cache = route.name in ("static", "feed", "file", "ftp") and not merging
        req = FetchRequest(
            url=url,
            resolved=resolved,
            settings=self.e.settings,
            etag=ctx.latest["etag"] if ctx.latest and use_cache else None,
            last_modified=ctx.latest["last_modified"] if ctx.latest and use_cache else None,
            force=force or merging,
        )
        result = await self.e.fetchers.get(route).fetch(req)
        if merging and result.error is None and not result.not_modified:
            result = await self._merge(ctx, resolved, req, result, route)
        return result

    async def _merge(
        self, ctx: _Context, resolved: Resolved, req: FetchRequest, first: FetchResult, route: Route
    ) -> FetchResult:
        """Merge pages: follow the next-page link (or the URL pattern) and let the plugin add
        more, then concatenate before extraction (spec: Multi-page sources)."""
        e = self.e
        fetcher = e.fetchers.get(route)
        cfg = resolved.fetch.merge
        parts: list[tuple[bytes, str]] = [(first.body, first.content_type)]
        elapsed = first.elapsed_ms
        if cfg is not None and cfg.enabled and _htmlish(first.content_type, first.body):
            seen = {normalize_url(first.final_url)}
            seen_bodies = {sha256_hex(first.body)}
            last, number, total = first, cfg.first_n, len(first.body)
            for _ in range(cfg.max_pages - 1):
                if cfg.next_selector:
                    nxt = await e.pool.run(
                        next_link, last.body, last.content_type, last.final_url, cfg.next_selector
                    )
                else:
                    assert cfg.url_pattern
                    nxt = urljoin(last.final_url, cfg.url_pattern.replace("{n}", str(number)))
                    number += 1
                if not nxt or normalize_url(nxt) in seen:
                    break
                seen.add(normalize_url(nxt))
                if e.settings.per_host_min_gap_s > 0:
                    await e.clock.sleep(e.settings.per_host_min_gap_s)  # stay polite to the host
                page = await fetcher.fetch(replace(req, url=nxt, etag=None, last_modified=None,
                                                   force=True))  # fmt: skip
                elapsed += page.elapsed_ms
                if page.error is not None:
                    gone = page.error.status in (404, 410) and not cfg.next_selector
                    if gone:  # a URL pattern ran past the last page
                        break
                    return replace(page, elapsed_ms=elapsed)  # a partial merge must not be stored
                digest = sha256_hex(page.body)
                if digest in seen_bodies:  # the site repeats its last page
                    break
                seen_bodies.add(digest)
                total += len(page.body)
                if total > resolved.fetch.max_bytes:
                    err = FetchError(
                        FetchErrorKind.TOO_LARGE,
                        f"merged pages exceed {resolved.fetch.max_bytes} bytes",
                    )
                    return replace(first, error=err, elapsed_ms=elapsed)
                parts.append((page.body, page.content_type))
                last = page
        if ctx.plugin is not None and ctx.plugin.active:
            page_view = FetchedPage(
                first.final_url, first.status, dict(first.headers), first.content_type, first.body
            )
            parts += [(extra, "text/html; charset=utf-8") for extra in
                      await ctx.plugin.merge_pages(page_view)]  # fmt: skip
        if len(parts) == 1:
            return first
        body, content_type = await e.pool.run(merge_documents, parts)
        merged = replace(first, body=body, content_type=content_type, elapsed_ms=elapsed)
        merged.extra = {**first.extra, "merged_pages": str(len(parts))}
        merged.headers = {**first.headers, "content-type": content_type}
        return merged

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
        elapsed_ms: int,
        method: CheckKind,
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
            method=method.value,
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
        route: Route,
        switched: str | None = None,
    ) -> RunResult:
        e = self.e
        row = ctx.row
        method = method_kind(route, res.source_kind)
        if res.kind in ("unchanged_raw", "unchanged"):
            out = await self._commit_unchanged(ctx, resolved, run_id, fetch, res.elapsed_ms, method)
            out.browser = True if switched else None
            return out

        now = e.clock.now()
        base: dict[str, Any] = {
            "bookmark_id": row["id"],
            "run_id": run_id,
            "finished_at": iso(now),
            "duration_ms": fetch.elapsed_ms + res.elapsed_ms,
            "byte_count": len(fetch.body),
            "method": method.value,
            "check_method": "browser" if switched else None,
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
            return RunResult(due, browser=True if switched else None)

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
            screenshot_hash=res.screenshot_hash,
        )
        change: repo.NewChange | None = None
        outcome = Outcome.SUPPRESSED
        reason = res.reason
        is_first = res.kind == "first"
        if is_first:
            outcome, reason = Outcome.FIRST, (f"auto_browser:{switched}" if switched else None)
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
        return RunResult(due, browser=True if switched else None)

    async def _commit_skipped(self, ctx: _Context, resolved: Resolved, run_id: int) -> RunResult:
        """A plugin's ``before_check`` asked to skip this check: nothing is fetched or counted."""
        due, interval = self._next(ctx, resolved, changed=None)
        commit = repo.CheckCommit(
            bookmark_id=ctx.row["id"],
            run_id=run_id,
            finished_at=iso(self.e.clock.now()),
            outcome=Outcome.SKIPPED,
            reason="plugin_skip",
            duration_ms=0,
            byte_count=None,
            next_due_at=iso(due) if due else None,
            current_interval_s=interval,
            consecutive_errors=int(ctx.row["consecutive_errors"]),
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
        route: Route | None = None,
    ) -> RunResult:
        e = self.e
        row = ctx.row
        host = host_of(row["url"])
        if err.retry_after_s:
            e.host_gate.backoff(host, err.retry_after_s)
        # A failure that looks like a network problem may be ours, not the site's: probe before
        # counting it. A local file never needs the network.
        if (
            e.connectivity is not None
            and e.online
            and err.kind in NETWORK_KINDS
            and (route is None or route.name != "file")
        ):
            await e.connectivity.verify()
        if not e.online:
            return await self._skip_offline(ctx, run_id, elapsed_ms, err, route)
        threshold = resolved.gate.error_threshold if resolved else 3
        errors = int(row["consecutive_errors"])
        status: BookmarkStatus | None = None
        next_trigger = Trigger.SCHEDULE
        counted = True
        if err.transient and trigger is not Trigger.RETRY:
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
            method=route.kind.value if route is not None else None,
        )
        await self._write(ctx, commit, error=err)
        if became_error:
            e.notify_problem(
                row["id"], f"{row['name']} is failing", f"{err.reason}: {err.message}"[:200]
            )
        return RunResult(due, next_trigger)

    async def _skip_offline(
        self, ctx: _Context, run_id: int, elapsed_ms: int, err: FetchError, route: Route | None
    ) -> RunResult:
        """The engine is offline (spec: "failures are not counted at all"): the run is recorded
        as skipped, the error counter and status are left alone, and the bookmark stays due so
        it is caught up, staggered, when the network is back."""
        e = self.e
        due = e.clock.now()
        commit = repo.CheckCommit(
            bookmark_id=ctx.row["id"],
            run_id=run_id,
            finished_at=iso(due),
            outcome=Outcome.SKIPPED,
            reason=f"offline:{err.reason}",
            duration_ms=elapsed_ms,
            byte_count=None,
            next_due_at=iso(due),
            current_interval_s=ctx.row["current_interval_s"],
            method=route.kind.value if route is not None else None,
        )
        await self._write(ctx, commit, error=err)
        return RunResult(due, Trigger.CATCHUP)

    async def _write(
        self, ctx: _Context, commit: repo.CheckCommit, error: FetchError | None = None
    ) -> repo.CommitResult:
        e = self.e
        if ctx.plugin is not None:  # a plugin's warning (it raised, it was slow) reaches the log
            commit.reason = ctx.plugin.reason(commit.reason)
        result = await e.db.write(lambda c: repo.commit_check(c, commit))
        row = ctx.row
        log.info(
            "check",
            bookmark_id=row["id"],
            method=commit.method or CheckKind.STATIC.value,
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
        if ctx.plugin is not None:
            await self._plugin_finished(ctx, commit, result, error)
        return result

    async def _plugin_finished(
        self,
        ctx: _Context,
        commit: repo.CheckCommit,
        result: repo.CommitResult,
        error: FetchError | None,
    ) -> None:
        """``on_change`` and ``after_check``; a plugin can never fail a check that was stored."""
        plugin = ctx.plugin
        assert plugin is not None
        change: ChangeInfo | None = None
        if result.change_id is not None:
            change_id = result.change_id
            row = await self.e.db.read(lambda c: repo.change_get(c, change_id))
            if row is not None:
                change = ChangeInfo(
                    id=row["id"], bookmark_id=row["bookmark_id"], detected_at=row["detected_at"],
                    summary=row["summary"], added_words=row["added_words"],
                    removed_words=row["removed_words"], changed_blocks=row["changed_blocks"],
                    checks_accumulated=row["checks_accumulated"],
                    keyword_hits=list(loads(row["keyword_hits_json"], []) or []),
                )  # fmt: skip
        run = RunInfo(
            bookmark_id=ctx.row["id"], outcome=commit.outcome.value, reason=commit.reason,
            method=commit.method, duration_ms=commit.duration_ms,
            changed=commit.new_version is not None and not commit.is_first,
            alerted=change is not None, error=error.message if error is not None else None,
        )  # fmt: skip
        try:
            await plugin.finish(run, change)
        except Exception:
            log.warning("plugin_finish_failed", bookmark_id=ctx.row["id"], exc_info=True)
