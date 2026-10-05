"""Effective bookmark configuration: ``global defaults <- folder chain <- bookmark overrides``.

The JSON columns of a bookmark hold only the overrides the user set. This module merges them
with the defaults inherited through the folder chain and validates the result with the
shared models.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from pagewatch.models import (
    ActionsConfig,
    FetchConfig,
    FilterConfig,
    GateConfig,
    ScheduleConfig,
    Settings,
    deep_merge,
    loads,
)

SECTIONS = ("schedule", "fetch", "filter", "gate", "actions")
# Folder defaults may also carry these bookmark scalars; they are copied at creation time.
FOLDER_SCALAR_DEFAULTS = ("check_method", "source_type", "highlight_mode", "priority")


class FolderCache:
    """All folders in memory (a few hundred at most): id -> (parent_id, defaults)."""

    def __init__(self) -> None:
        self._folders: dict[int, tuple[int | None, dict[str, Any]]] = {}

    def load(self, rows: list[sqlite3.Row]) -> None:
        self._folders = {
            r["id"]: (r["parent_id"], loads(r["defaults_json"], {}) or {}) for r in rows
        }

    def chain_defaults(self, folder_id: int | None) -> dict[str, Any]:
        """Defaults merged root -> leaf (the nearest folder wins)."""
        chain: list[dict[str, Any]] = []
        seen: set[int] = set()
        cur = folder_id
        while cur is not None and cur in self._folders and cur not in seen:
            seen.add(cur)
            parent, defaults = self._folders[cur]
            chain.append(defaults)
            cur = parent
        merged: dict[str, Any] = {}
        for d in reversed(chain):
            merged = deep_merge(merged, d)
        return merged

    def exists(self, folder_id: int) -> bool:
        return folder_id in self._folders

    def is_ancestor(self, ancestor: int, folder_id: int | None) -> bool:
        """True if ``ancestor`` is ``folder_id`` or one of its ancestors (cycle guard)."""
        seen: set[int] = set()
        cur = folder_id
        while cur is not None and cur not in seen:
            if cur == ancestor:
                return True
            seen.add(cur)
            cur = self._folders.get(cur, (None, {}))[0]
        return False


@dataclass(slots=True)
class Resolved:
    schedule: ScheduleConfig
    fetch: FetchConfig
    filter: FilterConfig
    gate: GateConfig
    actions: ActionsConfig
    overrides: dict[str, Any]


def _section_defaults(section: str, settings: Settings) -> dict[str, Any]:
    if section == "schedule":
        dumped: dict[str, Any] = settings.default_schedule.model_dump(mode="json")
        return dumped
    return {}


def effective_section(
    section: str, row: sqlite3.Row, folders: FolderCache, settings: Settings
) -> dict[str, Any]:
    stored = loads(row[f"{section}_json"], {})
    if section == "actions" and isinstance(stored, list):
        stored = {"actions": stored}
    folder = folders.chain_defaults(row["folder_id"]).get(section, {})
    if section == "actions" and isinstance(folder, list):
        folder = {"actions": folder}
    return deep_merge(deep_merge(_section_defaults(section, settings), folder or {}), stored or {})


def resolve(row: sqlite3.Row, folders: FolderCache, settings: Settings) -> Resolved:
    """Raises ``pydantic.ValidationError`` if the merged configuration is invalid."""
    eff = {s: effective_section(s, row, folders, settings) for s in SECTIONS}
    stored = {s: loads(row[f"{s}_json"], {}) for s in SECTIONS}
    return Resolved(
        schedule=ScheduleConfig.model_validate(eff["schedule"]),
        fetch=FetchConfig.model_validate(eff["fetch"]),
        filter=FilterConfig.model_validate(eff["filter"]),
        gate=GateConfig.model_validate(eff["gate"]),
        actions=ActionsConfig.model_validate(eff["actions"]),
        overrides=stored,
    )


def resolve_candidate(
    row: sqlite3.Row,
    patch: dict[str, dict[str, Any] | None],
    folders: FolderCache,
    settings: Settings,
) -> Resolved:
    """The configuration the bookmark would have after ``PATCH`` with these section patches,
    without saving anything (test filter)."""
    from pagewatch.models import apply_patch

    stored = {s: loads(row[f"{s}_json"], {}) or {} for s in SECTIONS}
    stored["actions"] = (
        {"actions": stored["actions"]} if isinstance(stored["actions"], list) else stored["actions"]
    )
    for name, value in patch.items():
        if value is not None:
            stored[name] = apply_patch(stored[name], value)
    inherited = folders.chain_defaults(row["folder_id"])
    eff = {
        s: deep_merge(
            deep_merge(_section_defaults(s, settings), inherited.get(s, {}) or {}), stored[s]
        )
        for s in SECTIONS
    }
    return Resolved(
        schedule=ScheduleConfig.model_validate(eff["schedule"]),
        fetch=FetchConfig.model_validate(eff["fetch"]),
        filter=FilterConfig.model_validate(eff["filter"]),
        gate=GateConfig.model_validate(eff["gate"]),
        actions=ActionsConfig.model_validate(eff["actions"]),
        overrides=stored,
    )


def resolve_schedule(
    row: sqlite3.Row, folders: FolderCache, settings: Settings
) -> ScheduleConfig | None:
    """Just the effective schedule of a bookmark (the row needs only ``schedule_json`` and
    ``folder_id``). ``None`` when the stored configuration is invalid."""
    try:
        return ScheduleConfig.model_validate(effective_section("schedule", row, folders, settings))
    except ValidationError:
        return None


def source_options(fetch: FetchConfig) -> dict[str, Any]:
    """The part of a bookmark's fetch config the worker pipeline needs to read its content
    (records layout, feed options): small, picklable, and nothing secret."""
    out: dict[str, Any] = {"feed": fetch.feed.model_dump(mode="json")}
    if fetch.records is not None:
        out["records"] = fetch.records.model_dump(mode="json")
    return out


def validate_sections(
    sections: dict[str, Any],
    folder_id: int | None,
    folders: FolderCache,
    settings: Settings,
    source_type: str | None = None,
) -> None:
    """Validate would-be stored overrides against the defaults they will be merged with.
    A ``records`` source must end up with a ``fetch.records`` configuration."""
    models: dict[str, type[BaseModel]] = {
        "schedule": ScheduleConfig,
        "fetch": FetchConfig,
        "filter": FilterConfig,
        "gate": GateConfig,
        "actions": ActionsConfig,
    }
    inherited = folders.chain_defaults(folder_id)
    for name, model in models.items():
        if name not in sections:
            continue
        value = sections[name]
        if name == "actions" and isinstance(value, list):
            value = {"actions": value}
        base = deep_merge(_section_defaults(name, settings), inherited.get(name, {}) or {})
        model.model_validate(deep_merge(base, value or {}))
    if source_type == "records":
        fetch = deep_merge(inherited.get("fetch", {}) or {}, sections.get("fetch", {}) or {})
        if not fetch.get("records"):
            raise ValueError("a records source needs a 'records' configuration (fetch.records)")
