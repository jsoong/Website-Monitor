from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest

from pagewatch.engine.actions.toast import LogToastBackend
from pagewatch.engine.clock import FakeClock
from pagewatch.engine.core import Engine
from pagewatch.engine.paths import DataDir
from pagewatch.engine.tray import (
    NullTrayBackend,
    PystrayBackend,
    TrayController,
    TrayState,
    _wrap,
    compute_state,
    make_icon,
    tooltip,
)
from tests.support.fixture_site import FixtureSite, article
from tests.support.sim import advance, settle


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, TrayState.NORMAL),
        ({"unread": 3}, TrayState.UNREAD),
        ({"errors": 1, "unread": 3}, TrayState.ERROR),
        ({"paused": True, "errors": 1, "unread": 3}, TrayState.PAUSED),
        ({"online": False, "paused": True, "errors": 1}, TrayState.OFFLINE),
    ],
)
def test_state_precedence(kw: dict[str, Any], expected: TrayState) -> None:
    base = {"online": True, "paused": False, "errors": 0, "unread": 0}
    assert compute_state(**{**base, **kw}) == expected


def test_tooltips_are_informative() -> None:
    assert tooltip(TrayState.UNREAD, 1, 0) == "PageWatch: 1 unread change"
    assert "3 unread changes" in tooltip(TrayState.UNREAD, 3, 0)
    assert "2 bookmarks failing" in tooltip(TrayState.ERROR, 0, 2)


def test_icons_differ_per_state_and_theme_and_are_square_rgba() -> None:
    seen = set()
    for state in TrayState:
        img = make_icon(state)
        assert img.size == (64, 64) and img.mode == "RGBA"
        seen.add(img.tobytes())
    assert len(seen) == len(TrayState)
    assert make_icon(TrayState.NORMAL, dark=True).tobytes() != make_icon(TrayState.NORMAL).tobytes()
    assert make_icon(TrayState.ERROR, size=32).size == (32, 32)


@pytest.fixture
def backend() -> NullTrayBackend:
    return NullTrayBackend()


@pytest.fixture
def launched() -> list[int]:
    return []


@pytest.fixture
async def engine(  # overrides conftest's: the API and the tray must share one engine
    data_dir: DataDir,
    clock: FakeClock,
    settings_overrides: dict[str, Any],
    toasts: LogToastBackend,
    backend: NullTrayBackend,
    launched: list[int],
) -> AsyncIterator[Engine]:
    eng = Engine(
        data_dir, clock=clock, worker_mode="thread", settings_overrides=settings_overrides,
        toast_backend=toasts, tray_backend=backend,
    )  # fmt: skip
    assert eng.tray is not None
    eng.tray._launch = lambda: launched.append(1)
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def test_tray_follows_the_engine_state_through_events(
    engine: Engine, backend: NullTrayBackend, client: httpx.AsyncClient,
    clock: FakeClock, site: FixtureSite,
) -> None:  # fmt: skip
    eng = engine
    assert backend.started and backend.state is TrayState.NORMAL
    site.set("/a", article("one"))
    r = await client.post(
        "/bookmarks", json={"url": site.url("/a"), "schedule": {"interval_s": 60, "jitter_pct": 0}}
    )
    bid = r.json()["id"]
    await settle(eng, clock)
    site.set("/a", article("two"))
    await advance(eng, clock, 70, step=10)
    await asyncio.sleep(0.05)  # the controller reacts to change_detected on the event loop
    assert backend.state is TrayState.UNREAD and "1 unread change" in backend.tip

    await client.post(f"/bookmarks/{bid}/read")
    await asyncio.sleep(0.05)
    assert backend.state is TrayState.NORMAL

    await client.post("/autowatch", json={"state": "paused"})
    await asyncio.sleep(0.05)
    assert backend.state is TrayState.PAUSED
    await client.post("/autowatch", json={"state": "running"})
    eng.online = False
    await eng.tray.refresh()  # type: ignore[union-attr]
    assert backend.state is TrayState.OFFLINE
    eng.online = True
    await client.post(
        "/bookmarks",
        json={
            "url": site.url("/gone"),
            "schedule": {"interval_s": 60, "jitter_pct": 0},
            "gate": {"error_threshold": 1},
        },
    )
    await settle(eng, clock)
    await asyncio.sleep(0.05)
    assert backend.state is TrayState.ERROR
    assert backend.history[0] is TrayState.NORMAL and TrayState.UNREAD in backend.history


async def test_menu_actions(
    engine: Engine, backend: NullTrayBackend, launched: list[int], client: httpx.AsyncClient,
    clock: FakeClock, site: FixtureSite,
) -> None:  # fmt: skip
    eng = engine
    assert [label for label, _ in backend.menu] == [
        "Open PageWatch", "Check all now", "Pause AutoWatch for 1 hour",
        "Pause AutoWatch until resumed", "Resume AutoWatch", "Quit engine",
    ]  # fmt: skip
    site.set("/m", article("x"))
    await client.post("/bookmarks", json={"url": site.url("/m"), "schedule": {"mode": "manual"}})
    await settle(eng, clock)
    hits = len(site.hits)

    backend.click("Open PageWatch")
    backend.click("Check all now")
    await asyncio.sleep(0.05)
    await settle(eng, clock)
    assert launched == [1] and len(site.hits) == hits + 1

    # menu callbacks arrive on another thread: simulate that, they must hop onto the loop
    await asyncio.to_thread(backend.click, "Pause AutoWatch for 1 hour")
    await asyncio.sleep(0.05)
    assert eng.scheduler.paused and eng.scheduler.paused_until is not None
    assert eng.scheduler.paused_until - clock.now() == timedelta(hours=1)
    assert backend.state is TrayState.PAUSED
    backend.click("Resume AutoWatch")
    await asyncio.sleep(0.05)
    assert not eng.scheduler.paused and backend.state is TrayState.NORMAL
    backend.click("Pause AutoWatch until resumed")
    await asyncio.sleep(0.05)
    assert eng.scheduler.paused and eng.scheduler.paused_until is None

    backend.click("Quit engine")
    await asyncio.sleep(0.05)
    await asyncio.wait_for(eng.wait_stopped(), 2)


# -- pystray's callback arity ----------------------------------------------------------------


def _fake_pystray() -> types.ModuleType:
    """A stand-in for ``pystray`` (a Windows-only dependency) that enforces the same rule as
    ``pystray._base._assert_action``: an action's ``__code__.co_argcount`` must be 0, 1 or 2."""
    mod = types.ModuleType("pystray")

    class MenuItem:
        def __init__(self, text: str, action: Callable[..., Any]) -> None:
            if action.__code__.co_argcount not in (0, 1, 2):
                raise ValueError(action)
            self.text, self.action = text, action

    class Menu:
        def __init__(self, *items: MenuItem) -> None:
            self.items = items

    class Icon:
        def __init__(self, name: str, icon: Any, title: str, menu: Menu) -> None:
            self.menu, self.icon, self.title, self.ran = menu, icon, title, False

        def run_detached(self) -> None:
            self.ran = True

        def stop(self) -> None:
            self.ran = False

    mod.MenuItem, mod.Menu, mod.Icon = MenuItem, Menu, Icon  # type: ignore[attr-defined]
    return mod


def test_every_tray_menu_action_has_a_callback_arity_pystray_accepts() -> None:
    """On Windows ``lambda _i, _m, cb=cb: cb()`` (co_argcount == 3, the default argument counts)
    made pystray raise ValueError when the tray started."""
    calls: list[str] = []
    menu = [(label, lambda label=label: calls.append(label)) for label in ("a", "b", "c")]
    for label, cb in menu:
        action = _wrap(cb)
        assert action.__code__.co_argcount in (0, 1, 2), label
        action(object(), object())  # pystray calls it with (icon, item)
    assert calls == ["a", "b", "c"]  # each wrapper is bound to its own callback


def test_pystray_backend_registers_every_controller_menu_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_pystray()
    monkeypatch.setitem(sys.modules, "pystray", fake)
    # TrayController.menu() only builds closures: no engine is touched until one is clicked
    entries = TrayController.menu(types.SimpleNamespace())  # type: ignore[arg-type]
    assert len(entries) == 6
    backend = PystrayBackend()
    backend.start(entries)  # raised ValueError("<lambda>") before the fix
    icon = backend._icon
    assert icon.ran and [i.text for i in icon.menu.items] == [label for label, _ in entries]
    for item in icon.menu.items:
        assert item.action.__code__.co_argcount in (0, 1, 2), item.text
