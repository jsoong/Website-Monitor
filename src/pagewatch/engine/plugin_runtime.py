"""Plugin runtime: loading plugin files and calling their hooks (spec: Extensibility).

This is the part that also runs in worker processes (the pipeline hooks ``preprocess``,
``compare`` and ``check_keywords`` run there), so it imports as little as possible: no database,
no bookmark service.

* ``load`` imports a plugin file (cached per process, reloaded when the file changes) and reports
  which hooks it defines, or why it cannot be loaded.
* ``Session`` calls hooks for one bookmark. A hook that raises is logged and treated as absent; one
  slower than ``SLOW_HOOK_S`` leaves a warning. State, property changes and notifications a hook
  made are collected and travel back to the engine as a ``PluginOutcome``.

Notes that belong in the Check log (``plugin_error:<hook>``, ``plugin_slow:<hook>``) are collected
on the session; the engine records the first one in ``check_run.reason`` when nothing more
important is.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from pagewatch.engine.clock import parse_iso
from pagewatch.models import PLUGIN_HOOKS
from pagewatch.plugin_api import BookmarkView, PluginContext, Verdict

SLOW_HOOK_S = 5.0
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,59}$")


class _Absent:
    def __repr__(self) -> str:
        return "<hook absent>"


ABSENT: Any = _Absent()


# -- loading ----------------------------------------------------------------------------


@dataclass(slots=True)
class Loaded:
    path: str
    mtime_ns: int
    module: ModuleType | None
    hooks: frozenset[str]
    error: str | None = None

    @property
    def description(self) -> str | None:
        doc = getattr(self.module, "__doc__", None) if self.module else None
        if not doc:
            return None
        first = next((ln.strip() for ln in doc.strip().splitlines() if ln.strip()), "")
        return first or None


_CACHE: dict[str, Loaded] = {}
_LOCK = threading.Lock()


def load(path: str | Path) -> Loaded:
    """Import ``path`` (once per version of the file)."""
    key = str(path)
    try:
        mtime = os.stat(key).st_mtime_ns
    except OSError as exc:
        return Loaded(key, 0, None, frozenset(), f"cannot read the plugin: {exc.strerror or exc}")
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.mtime_ns == mtime:
            return cached
        module_name = "pagewatch_plugin_" + re.sub(r"\W", "_", Path(key).stem)
        spec = importlib.util.spec_from_file_location(module_name, key)
        if spec is None or spec.loader is None:
            loaded = Loaded(key, mtime, None, frozenset(), "not a Python module")
        else:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module  # dataclasses and pickling look the module up
            try:
                spec.loader.exec_module(module)
            except BaseException as exc:
                sys.modules.pop(module_name, None)
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                loaded = Loaded(key, mtime, None, frozenset(), f"{type(exc).__name__}: {exc}"[:300])
            else:
                hooks = frozenset(h for h in PLUGIN_HOOKS if callable(getattr(module, h, None)))
                loaded = Loaded(key, mtime, module, hooks)
        _CACHE[key] = loaded
        return loaded


# -- one bookmark's session -------------------------------------------------------------


@dataclass(slots=True)
class PluginSpec:
    """Everything a worker needs to run a plugin's hooks for one bookmark (picklable)."""

    name: str
    path: str
    bookmark: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    now: str = ""
    user_agent: str = ""


@dataclass(slots=True)
class PluginOutcome:
    """What a worker-side session hands back to the engine."""

    state: dict[str, Any] | None = None  # None: unchanged
    props: dict[str, str | None] = field(default_factory=dict)
    alerts: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    logs: list[tuple[str, str]] = field(default_factory=list)


class Session:
    """Calls the hooks of one plugin for one bookmark."""

    def __init__(self, spec: PluginSpec, emit: Callable[[str, str], None] | None = None) -> None:
        self.spec = spec
        self.loaded = load(spec.path)
        self.notes: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self._emit = emit
        now: datetime | None = None
        if spec.now:
            try:
                now = parse_iso(spec.now)
            except ValueError:
                now = None
        self._initial = json.dumps(spec.state, sort_keys=True)
        self.ctx = PluginContext(
            bookmark=BookmarkView.from_dict(spec.bookmark),
            state=copy.deepcopy(spec.state),
            user_agent=spec.user_agent,
            _now=now,
            _log=self._log,
        )
        if self.loaded.error:
            self._note("plugin_error:load", f"cannot load {spec.name}: {self.loaded.error}")

    # -- bookkeeping ----------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        self.logs.append((level, message))
        if self._emit is not None:
            self._emit(level, message)

    def _note(self, code: str, message: str) -> None:
        if code not in self.notes:
            self.notes.append(code)
        self._log("warning", f"{self.spec.name}: {message}")

    def has(self, hook: str) -> bool:
        return hook in self.loaded.hooks

    def state_changed(self) -> bool:
        return json.dumps(self.ctx.state, sort_keys=True, default=str) != self._initial

    def outcome(self) -> PluginOutcome:
        return PluginOutcome(
            state=copy.deepcopy(self.ctx.state) if self.state_changed() else None,
            props=dict(self.ctx._props),
            alerts=list(self.ctx._alerts),
            notes=list(self.notes),
            logs=list(self.logs),
        )

    # -- calling a hook -------------------------------------------------------------------

    def call(self, hook: str, *args: Any) -> Any:
        """Run ``hook(ctx, *args)``. ``ABSENT`` if the plugin has no such hook or it raised."""
        if not self.has(hook) or self.loaded.module is None:
            return ABSENT
        fn = getattr(self.loaded.module, hook)
        self.ctx._http_calls = 0
        started = time.perf_counter()
        try:
            value = fn(self.ctx, *args)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError("a hook must be a plain function, not a coroutine")
        except Exception as exc:
            self._note(f"plugin_error:{hook}", f"{hook} raised {type(exc).__name__}: {exc}")
            return ABSENT
        finally:
            elapsed = time.perf_counter() - started
            if elapsed > SLOW_HOOK_S:
                self._note(
                    f"plugin_slow:{hook}", f"{hook} took {elapsed:.1f}s (limit {SLOW_HOOK_S}s)"
                )
        return value

    # -- the hooks the pipeline runs ------------------------------------------------------

    def preprocess(self, text: str) -> str:
        if not self.has("preprocess"):
            return text
        out = self.call("preprocess", text)
        if out is ABSENT or out is None:
            return text
        if not isinstance(out, str):
            self._note("plugin_error:preprocess", "preprocess must return a string")
            return text
        return out

    def check_keywords(self, added_text: str) -> bool | None:
        value = self.call("check_keywords", added_text)
        if value is ABSENT or value is None:
            return None
        return bool(value)

    def compare(self, old: list[str], new: list[str], stats: dict[str, int]) -> Verdict | None:
        self.ctx.diff = dict(stats)
        value = self.call("compare", old, new)
        if value is ABSENT or value is None:
            return None
        if isinstance(value, Verdict):
            return value
        if isinstance(value, bool):
            return Verdict.change() if value else Verdict.no_change()
        self._note("plugin_error:compare", "compare must return a Verdict, a bool or None")
        return None


def session_of(plugin: Any) -> Session | None:
    """A ``Session`` from what travels in ``source_cfg['plugin']`` (a spec dict, or a live session
    that a worker job shares across its steps)."""
    if plugin is None:
        return None
    if isinstance(plugin, Session):
        return plugin
    if isinstance(plugin, dict):
        return Session(PluginSpec(**plugin))
    return None
