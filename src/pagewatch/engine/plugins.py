"""Plugin host: the engine's side of plugins (spec: Extensibility → Plugin hooks).

Discovery and installation of the shipped examples, persistent per-bookmark state, hooks that run
in a thread so a slow plugin cannot stall the event loop, notifications and property changes.
The hooks that run in worker processes live in ``plugin_runtime``.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import tempfile
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import kv
from pagewatch.engine.clock import iso
from pagewatch.engine.logs import get_logger
from pagewatch.engine.plugin_runtime import (
    ABSENT,
    NAME,
    PluginOutcome,
    PluginSpec,
    Session,
    load,
)
from pagewatch.models import AlertPrivacy, BookmarkPatch, PluginInfo
from pagewatch.plugin_api import ChangeInfo, CheckRequest, FetchedPage, RunInfo

if TYPE_CHECKING:
    import sqlite3

    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.plugins")


class PluginExistsError(ValueError):
    """Installing a plugin would overwrite a file that is already there."""


# -- the engine's side ------------------------------------------------------------------


def _examples_dir() -> Any:
    return resources.files("pagewatch.data").joinpath("plugins")


def example_names() -> list[str]:
    root = _examples_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name[:-3] for p in root.iterdir() if p.name.endswith(".py") and not p.name.startswith("_")
    )


class PluginHost:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    @property
    def directory(self) -> Path:
        return self.e.data_dir.plugins_dir

    def path_for(self, name: str | None) -> Path | None:
        """The plugin's file, only ever inside the plugins folder."""
        if not name or not NAME.match(name):
            return None
        path = self.directory / f"{name}.py"
        return path if path.is_file() else None

    def exists(self, name: str) -> bool:
        return self.path_for(name) is not None

    # -- catalogue ------------------------------------------------------------------------

    def info(self, name: str) -> PluginInfo:
        path = self.path_for(name)
        shipped = name in example_names()
        about = self._example_description(name) if shipped else None
        if path is None:
            return PluginInfo(name=name, installed=False, example=shipped, description=about)
        loaded = load(path)
        return PluginInfo(
            name=name,
            installed=True,
            example=shipped,
            hooks=sorted(loaded.hooks),
            description=loaded.description or about,
            error=loaded.error,
        )

    @staticmethod
    def _example_description(name: str) -> str | None:
        text = _examples_dir().joinpath(f"{name}.py").read_text(encoding="utf-8")
        m = re.match(r'\s*"""(.*?)"""', text, re.S)
        if not m:
            return None
        return next((ln.strip() for ln in m.group(1).splitlines() if ln.strip()), None)

    def catalogue(self) -> list[PluginInfo]:
        """Installed plugins and the shipped examples (blocking: imports the installed files)."""
        names = set(example_names())
        if self.directory.is_dir():
            for p in self.directory.glob("*.py"):
                if NAME.match(p.stem):
                    names.add(p.stem)
        return [self.info(n) for n in sorted(names)]

    def install(self, name: str, *, overwrite: bool = False) -> PluginInfo:
        """Copy a shipped example into the plugins folder."""
        if name not in example_names():
            have = ", ".join(example_names()) or "none"
            raise svc.NotFoundError(f"there is no shipped plugin named {name!r} (shipped: {have})")
        target = self.directory / f"{name}.py"
        if target.exists() and not overwrite:
            raise PluginExistsError(f"{target.name} is already installed (overwrite to replace it)")
        source = _examples_dir().joinpath(f"{name}.py").read_text(encoding="utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(source)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.info(name)

    # -- sessions -------------------------------------------------------------------------

    def _spec(self, row: sqlite3.Row, state: dict[str, Any]) -> PluginSpec | None:
        name = row["plugin"]
        path = self.path_for(name)
        if path is None:
            return None
        bookmark = {
            f: row[f]
            for f in (
                "id", "name", "url", "folder_id", "source_type", "check_method",
                "info1", "info2", "info3", "note", "plugin", "status",
            )
        }  # fmt: skip
        return PluginSpec(
            name=name, path=str(path), bookmark=bookmark, state=state,
            now=iso(self.e.clock.now()), user_agent=self.e.settings.default_user_agent,
        )  # fmt: skip

    def preprocess_spec(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """A spec for views and re-normalising: only plugins with a ``preprocess`` hook matter
        there (no state: nothing but a check changes it)."""
        spec = self._spec(row, {}) if row["plugin"] else None
        if spec is None or "preprocess" not in load(spec.path).hooks:
            return None
        return asdict(spec)

    async def open(self, row: sqlite3.Row) -> EngineSession | None:
        """The session for a bookmark's check, or ``None`` (no plugin, or its file is gone)."""
        name = row["plugin"]
        if not name:
            return None
        state = await self.e.db.read(lambda c: kv.get(c, kv.PLUGIN_STATE, row["id"], {}))
        spec = self._spec(row, state if isinstance(state, dict) else {})
        if spec is None:
            log.warning("plugin_missing", bookmark_id=row["id"], plugin=name)
            return EngineSession(self, row, None, missing=name)
        return EngineSession(self, row, spec)


class EngineSession:
    """A plugin's hooks for one check, on the engine side."""

    def __init__(
        self, host: PluginHost, row: sqlite3.Row, spec: PluginSpec | None, missing: str = ""
    ) -> None:
        self.host = host
        self.row = row
        self.spec = spec
        self.notes: list[str] = [f"plugin_missing:{missing}"] if missing else []
        self.session: Session | None = None
        self._props: dict[str, str | None] = {}
        self._worker_alerts: list[tuple[str, str]] = []
        if spec is not None:
            self.session = Session(spec, emit=self._emit)

    def _emit(self, level: str, message: str) -> None:
        getattr(log, "warning" if level == "warning" else "info")(
            "plugin", bookmark_id=self.row["id"], message=message
        )

    @property
    def active(self) -> bool:
        return self.session is not None

    def spec_for_worker(self) -> dict[str, Any] | None:
        """The current state as a spec dict for ``source_cfg['plugin']``; ``None`` when the
        plugin has no hook that runs in a worker."""
        s = self.session
        if s is None or not (s.has("preprocess") or s.has("compare") or s.has("check_keywords")):
            return None
        spec = asdict(self.spec) if self.spec is not None else None
        if spec is not None:
            spec["state"] = copy.deepcopy(s.ctx.state)
        return spec

    def all_notes(self) -> list[str]:
        merged = list(self.notes)
        if self.session is not None:
            merged += [n for n in self.session.notes if n not in merged]
        return merged

    def reason(self, reason: str | None) -> str | None:
        """``reason`` unless there is none, in which case the first plugin note (if any)."""
        if reason:
            return reason
        notes = self.all_notes()
        return notes[0] if notes else None

    # -- hooks on the engine ----------------------------------------------------------------

    async def hook(self, name: str, *args: Any) -> Any:
        """Run a hook in a thread (a plugin may block); alerts and property changes it made are
        handled afterwards."""
        s = self.session
        if s is None or not s.has(name):
            return ABSENT
        value = await asyncio.to_thread(s.call, name, *args)
        await self._flush()
        return value

    async def before_check(self, request: CheckRequest) -> bool:
        """False: skip this check."""
        s = self.session
        if s is None or not s.has("before_check"):
            return True
        s.ctx.request = request
        try:
            value = await self.hook("before_check")
        finally:
            s.ctx.request = None
        return value is not False

    async def merge_pages(self, page: FetchedPage) -> list[bytes]:
        """Extra HTML to append, as bytes."""
        value = await self.hook("merge_pages", page)
        if value is ABSENT or value is None:
            return []
        items = value if isinstance(value, list | tuple) else [value]
        out: list[bytes] = []
        for item in items:
            if isinstance(item, str):
                out.append(item.encode("utf-8"))
            elif isinstance(item, bytes | bytearray):
                out.append(bytes(item))
            else:
                self.notes.append("plugin_error:merge_pages")
                log.warning("plugin_merge_pages_type", plugin=self.row["plugin"])
        return out

    def absorb(self, outcome: PluginOutcome | None) -> None:
        """Take what a worker-side session did (state, properties, alerts, notes)."""
        if outcome is None or self.session is None:
            return
        if outcome.state is not None:
            self.session.ctx.state.clear()
            self.session.ctx.state.update(outcome.state)
        self._props.update(outcome.props)
        self._worker_alerts += outcome.alerts
        for note in outcome.notes:
            if note not in self.notes:
                self.notes.append(note)
        for level, message in outcome.logs:
            self._emit(level, message)

    async def after_pipeline(self, outcome: PluginOutcome | None) -> None:
        self.absorb(outcome)
        await self._flush()

    async def finish(self, run: RunInfo, change: ChangeInfo | None) -> None:
        """``on_change`` (if there was an alert), ``after_check``, then persist the state."""
        if self.session is None:
            return
        if change is not None:
            await self.hook("on_change", change)
        await self.hook("after_check", run)
        await self._save_state()

    # -- effects ----------------------------------------------------------------------------

    async def _flush(self) -> None:
        s = self.session
        if s is None:
            return
        alerts, s.ctx._alerts = s.ctx._alerts + self._worker_alerts, []
        self._worker_alerts = []
        self._props.update(s.ctx._props)
        s.ctx._props = {}
        e = self.host.e
        private = svc.privacy_of(self.row) is AlertPrivacy.PRIVATE
        for title, body in alerts:
            text = "A plugin raised an alert. Open PageWatch to review." if private else body
            e.events.publish(
                "plugin_alert", {"bookmark_id": self.row["id"], "title": title,
                                 "plugin": self.row["plugin"]},
            )  # fmt: skip
            e.spawn(
                e.toasts.notify(self.row["id"], title or self.row["name"], text), "plugin-toast"
            )

    async def _save_state(self) -> None:
        s = self.session
        e = self.host.e
        if s is not None and s.state_changed():
            state = copy.deepcopy(s.ctx.state)
            bid = self.row["id"]
            await e.db.write(lambda c: kv.put(c, kv.PLUGIN_STATE, bid, state))
        if self._props:
            props, self._props = self._props, {}
            try:
                await svc.patch(e, self.row["id"], BookmarkPatch.model_validate(props))
            except (svc.InvalidError, svc.NotFoundError, ValueError) as exc:
                log.warning(
                    "plugin_set_property_failed", bookmark_id=self.row["id"], error=str(exc)
                )
