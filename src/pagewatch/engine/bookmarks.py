"""Bookmark service: create, patch and delete with validation, folder-default inheritance,
scheduler synchronisation and events. The API, the CLI import and bulk edits all go here."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from pagewatch.engine import schedule as sched
from pagewatch.engine.clock import iso
from pagewatch.engine.config import FOLDER_SCALAR_DEFAULTS, resolve, validate_sections
from pagewatch.engine.store import repo
from pagewatch.models import (
    AlertPrivacy,
    BookmarkIn,
    BookmarkPatch,
    BookmarkStatus,
    apply_patch,
    dumps,
    loads,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

SECTIONS = ("schedule", "fetch", "filter", "gate", "actions")
_RESETS_VERSIONS = {"url"}
_REBUILD_FIELDS = {"filter", "highlight_mode", "source_type"}


class NotFoundError(LookupError):
    pass


class InvalidError(ValueError):
    pass


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first["loc"])
    return f"{loc}: {first['msg']}" if loc else str(first["msg"])


def _check(
    engine: Engine,
    sections: dict[str, Any],
    folder_id: int | None,
    source_type: str | None = None,
) -> None:
    try:
        validate_sections(sections, folder_id, engine.folders, engine.settings, source_type)
    except ValidationError as exc:
        raise InvalidError(_validation_message(exc)) from exc
    except ValueError as exc:
        raise InvalidError(str(exc)) from exc


def default_name(url: str) -> str:
    host = urlsplit(url).hostname
    return host or url[:80]


async def create(engine: Engine, body: BookmarkIn) -> sqlite3.Row:
    if body.folder_id is not None and not engine.folders.exists(body.folder_id):
        raise InvalidError(f"folder {body.folder_id} does not exist")
    sections: dict[str, Any] = {}
    for name in SECTIONS:
        value = getattr(body, name)
        if value is not None:
            sections[name] = value
    inherited = engine.folders.chain_defaults(body.folder_id)
    if "actions" not in sections and "actions" not in inherited:
        sections["actions"] = {"actions": [{"type": "toast"}]}  # toast is on by default
    source_type = (
        inherited["source_type"]
        if "source_type" not in body.model_fields_set and "source_type" in inherited
        else body.source_type.value
    )
    _check(engine, sections, body.folder_id, source_type)

    fields: dict[str, Any] = {
        "folder_id": body.folder_id,
        "name": body.name or default_name(body.url),
        "url": body.url,
        "enabled": int(body.enabled),
        "macro_id": body.macro_id,
        "plugin": body.plugin,
        "info1": body.info1,
        "info2": body.info2,
        "info3": body.info3,
        "note": body.note,
    }
    explicit = body.model_fields_set
    for scalar in FOLDER_SCALAR_DEFAULTS:
        if scalar not in explicit and scalar in inherited:
            fields[scalar] = inherited[scalar]
        else:
            value = getattr(body, scalar)
            fields[scalar] = getattr(value, "value", value)
    for name, value in sections.items():
        fields[f"{name}_json"] = dumps(value)
    fields.setdefault("schedule_json", "{}")  # NOT NULL without a default: inherit everything
    now = engine.clock.now()
    fields["next_due_at"] = iso(now) if body.enabled else None  # the first check runs at once
    if not body.enabled:
        fields["status"] = BookmarkStatus.DISABLED.value

    def write(conn: sqlite3.Connection) -> sqlite3.Row:
        bid = repo.bookmark_insert(conn, fields, iso(now))
        row = repo.bookmark_get(conn, bid)
        assert row is not None
        return row

    row = await engine.db.write(write)
    engine.schedule_bookmark(row)
    engine.events.publish("bookmark_updated", {"bookmark_id": row["id"], "created": True})
    return row


async def patch(engine: Engine, bookmark_id: int, body: BookmarkPatch) -> sqlite3.Row:
    row = await engine.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
    if row is None:
        raise NotFoundError(f"bookmark {bookmark_id} not found")
    explicit = body.model_fields_set
    fields: dict[str, Any] = {}
    changed: set[str] = set()

    for scalar in ("name", "source_type", "check_method", "highlight_mode"):
        if scalar in explicit and getattr(body, scalar) is not None:
            value = getattr(body, scalar)
            fields[scalar] = getattr(value, "value", value)
            changed.add(scalar)
    for scalar in ("priority", "macro_id", "plugin", "info1", "info2", "info3", "note"):
        if scalar in explicit:
            fields[scalar] = getattr(body, scalar)
    if "url" in explicit and body.url is not None:
        probe = BookmarkIn(url=body.url)  # reuses the url validation
        if probe.url != row["url"]:
            fields["url"] = probe.url
            changed.add("url")
    folder_id = row["folder_id"]
    if body.move_to_root:
        folder_id = None
        fields["folder_id"] = None
    elif "folder_id" in explicit and body.folder_id is not None:
        if not engine.folders.exists(body.folder_id):
            raise InvalidError(f"folder {body.folder_id} does not exist")
        folder_id = body.folder_id
        fields["folder_id"] = folder_id

    stored: dict[str, dict[str, Any]] = {}
    for name in SECTIONS:
        patch_value = getattr(body, name)
        if name in explicit and patch_value is not None:
            current = loads(row[f"{name}_json"], {})
            if name == "actions" and isinstance(current, list):
                current = {"actions": current}
            if name == "actions" and isinstance(patch_value, list):
                patch_value = {"actions": patch_value}  # a list replaces the actions wholesale
                new = apply_patch(current or {}, {"actions": patch_value["actions"]})
            else:
                new = apply_patch(current or {}, patch_value)
            stored[name] = new
            fields[f"{name}_json"] = dumps(new)
            changed.add(name)
    source_type = fields.get("source_type", row["source_type"])
    if "source_type" in changed and "fetch" not in stored:
        stored["fetch"] = loads(row["fetch_json"], {}) or {}  # a records source needs its config
    if stored:
        _check(engine, stored, folder_id, source_type)
    elif "folder_id" in fields:
        # moving between folders changes the inherited defaults: the result must still be valid
        _check(
            engine, {n: loads(row[f"{n}_json"], {}) or {} for n in SECTIONS}, folder_id, source_type
        )

    now = engine.clock.now()
    reset = bool(changed & _RESETS_VERSIONS)
    if reset:
        fields.update(
            latest_version_id=None, baseline_version_id=None, gate_anchor_version_id=None,
            status=BookmarkStatus.NEW.value, unread=0, consecutive_errors=0,
            current_interval_s=None, next_due_at=iso(now),
        )  # fmt: skip
    if "enabled" in explicit and body.enabled is not None:
        fields["enabled"] = int(body.enabled)
        if body.enabled:
            fields["next_due_at"] = fields.get("next_due_at") or iso(now)
            if row["status"] == BookmarkStatus.DISABLED.value:
                fields["status"] = (
                    BookmarkStatus.OK.value
                    if row["latest_version_id"]
                    else BookmarkStatus.NEW.value
                )
        else:
            fields["status"] = BookmarkStatus.DISABLED.value

    def write(conn: sqlite3.Connection) -> sqlite3.Row:
        repo.bookmark_update(conn, bookmark_id, fields, iso(now))
        if reset:
            conn.execute("DELETE FROM view_diff_cache WHERE bookmark_id=?", (bookmark_id,))
        out = repo.bookmark_get(conn, bookmark_id)
        assert out is not None
        return out

    updated = await engine.db.write(write)
    if "schedule" in changed and not reset and "enabled" not in explicit:
        resolved = resolve(updated, engine.folders, engine.settings)
        due, interval = sched.next_due(
            resolved.schedule, now, current_interval_s=None, changed=True,
            on_battery=engine.on_battery, zone=engine.zone,
        )  # fmt: skip
        await engine.db.write(
            lambda c: repo.bookmark_update(
                c,
                bookmark_id,
                {"next_due_at": iso(due) if due else None, "current_interval_s": interval},
                iso(now),
            )
        )
        refreshed = await engine.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        updated = refreshed if refreshed is not None else updated
    engine.schedule_bookmark(updated)
    if changed & _REBUILD_FIELDS and updated["latest_version_id"] and not reset:
        engine.spawn(engine.rebuild_versions(bookmark_id), f"rebuild-{bookmark_id}")
    engine.events.publish("bookmark_updated", {"bookmark_id": bookmark_id})
    return updated


async def delete(engine: Engine, ids: list[int]) -> int:
    n = await engine.db.write(lambda c: repo.bookmark_delete(c, ids))
    for bid in ids:
        engine.scheduler.remove(bid)
        engine.events.publish("bookmark_updated", {"bookmark_id": bid, "deleted": True})
    return n


def privacy_of(row: sqlite3.Row) -> AlertPrivacy:
    raw = loads(row["actions_json"], {})
    if isinstance(raw, dict) and raw.get("alert_privacy") == "private":
        return AlertPrivacy.PRIVATE
    return AlertPrivacy.CONTENT
