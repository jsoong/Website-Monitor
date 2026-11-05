"""An engine that restarts (after a restore, a memory-guard exit, or by hand) comes back on a new
port with a new token. The window must follow it instead of staying disconnected for good."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pagewatch.cli.client import ApiError
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.workers import wait_idle
from tests.support.engine_thread import EngineThread


def test_retarget_switches_the_client_to_the_new_engine(
    eng: EngineThread, api_client: ApiClient
) -> None:
    bad = ApiClient(eng.base, "not-the-token")
    try:
        with pytest.raises(ApiError) as caught:
            bad.health()
        assert caught.value.status == 401
        bad.retarget(eng.base, eng.engine.token)  # what a restart changes: address and token
        assert bad.health().pid > 0 and bad.token == eng.engine.token
    finally:
        bad.close()


def test_the_window_follows_a_restarted_engine_to_its_new_port_and_token(
    tmp_path: Path, qapp: Any, pump_until: Callable[..., None]
) -> None:
    root = tmp_path / "data"
    first = EngineThread(root).start()
    client = ApiClient(first.base, first.engine.token)
    events = EventStream(client.ws_url, client.token)
    window = MainWindow(client, events, data_dir=first.data_dir)
    second: EngineThread | None = None
    try:
        events.start()
        window.show()
        pump_until(lambda: events.connected, what="first connection")
        old = (first.port, first.engine.token)

        first.stop()
        pump_until(lambda: not events.connected, what="disconnect")
        assert window.lbl_conn.text() == "engine: disconnected"
        assert window.follow_restart() is False  # nothing is running: nothing to follow yet

        second = EngineThread(root).start()  # the same data folder, a new start
        assert (second.port, second.engine.token) != old
        pump_until(lambda: events.connected, timeout=30, what="reconnection to the new engine")
        assert client.token == second.engine.token and str(second.port) in client.base_url
        pump_until(lambda: window.lbl_conn.text() == "engine: connected", what="status text")
        assert window.follow_restart() is False  # already pointing at it
        pump_until(lambda: client.health().pid > 0, what="API answers")
    finally:
        wait_idle(2000)
        window.close()
        if second is not None:
            second.stop()


def test_a_window_without_a_data_folder_never_retargets(
    eng: EngineThread, api_client: ApiClient, qapp: Any
) -> None:
    window = MainWindow(api_client, None)  # the tests' usual construction: nothing to follow
    try:
        assert window.follow_restart() is False
    finally:
        wait_idle(2000)
        window.close()


def test_a_port_that_is_only_a_prefix_of_the_current_one_is_still_a_different_engine(
    eng: EngineThread, qapp: Any
) -> None:
    """Port 80 is not port 8080: the comparison is of the whole address, not a substring."""
    from pagewatch.engine.instance import write_lockfile
    from pagewatch.models import LockInfo

    stale = f"http://127.0.0.1:{eng.port}0"  # an address that merely starts with the live port
    client = ApiClient(stale, eng.engine.token)
    window = MainWindow(client, EventStream(client.ws_url, client.token), data_dir=eng.data_dir)
    try:
        assert eng.port and f":{eng.port}" in stale  # the old substring test would call this equal
        write_lockfile(
            eng.data_dir,
            LockInfo(
                pid=os.getpid(),
                port=eng.port,
                token=eng.engine.token,
                version="t",
                started_at="now",
            ),
        )
        assert window.follow_restart() is True and client.base_url == eng.base
        assert window.follow_restart() is False  # and now it really is the same engine
    finally:
        wait_idle(2000)
        window.close()
        client.close()
