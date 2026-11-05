"""M5: the Settings window, against a real engine (real HTTP and WebSocket, fake clock)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QDialog

from pagewatch.engine.logs import set_debug
from pagewatch.engine.paths import DataDir
from pagewatch.ui.client import ApiClient
from pagewatch.ui.events import EventStream
from pagewatch.ui.windows.main_window import MainWindow
from pagewatch.ui.windows.settings import (
    P_DATA,
    PAGES,
    SettingsDialog,
    duration_text,
    fmt_size,
    folder_sizes,
    list_backups,
    rate_text,
    tree_size,
)
from pagewatch.ui.workers import wait_idle
from tests.support.engine_thread import EngineThread
from tests.support.fixture_site import article


@pytest.fixture
def opened(
    eng: EngineThread, api_client: ApiClient, qapp: Any, pump_until: Callable[..., None]
) -> Iterator[Callable[[], SettingsDialog]]:
    dialogs: list[SettingsDialog] = []

    def factory() -> SettingsDialog:
        d = SettingsDialog(api_client, api_client.settings(), eng.data_dir)
        dialogs.append(d)
        pump_until(lambda: not d.last_backup.text().endswith("checking..."), what="health")
        return d

    yield factory
    wait_idle(2000)
    for d in dialogs:
        d.close()


def page(dialog: SettingsDialog, name: str) -> None:
    dialog.nav.setCurrentRow(PAGES.index(name))


# -- pure helpers --------------------------------------------------------------------------------


def test_sizes_are_human_readable_and_folders_are_summed(tmp_path: Path) -> None:
    assert fmt_size(0) == "0 bytes" and fmt_size(1023) == "1023 bytes"
    assert fmt_size(1536) == "1.5 KB" and fmt_size(5 * 1024**3) == "5.0 GB"
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "one").write_bytes(b"x" * 100)
    (tmp_path / "a" / "b" / "two").write_bytes(b"y" * 50)
    assert tree_size(tmp_path / "a") == 150 and tree_size(tmp_path / "a" / "one") == 100
    assert tree_size(tmp_path / "missing") == 0
    data = DataDir(tmp_path / "data").ensure()
    data.db_path.write_bytes(b"d" * 10)
    Path(f"{data.db_path}-wal").write_bytes(b"w" * 5)
    (data.blobs_dir / "ab").mkdir()
    (data.blobs_dir / "ab" / "blob").write_bytes(b"b" * 20)
    sizes = folder_sizes(data)
    assert sizes["Database"] == 15 and sizes["Page store"] == 20 and sizes["Backups"] == 0


def test_rates_and_durations_are_shown_without_false_precision() -> None:
    assert rate_text(1666.67) == "1,667/s" and rate_text(12.4) == "12/s"
    assert rate_text(0.0667) == "0.07/s" and rate_text(0) == "0.00/s"
    assert duration_text(0.002) == "2 ms" and duration_text(1.0) == "1.0 s"
    assert duration_text(90) == "90.0 s"


def test_backups_are_listed_newest_first_and_only_zips(tmp_path: Path) -> None:
    data = DataDir(tmp_path).ensure()
    old, new = data.backups_dir / "old.zip", data.backups_dir / "new.zip"
    old.write_bytes(b"1")
    new.write_bytes(b"2")
    import os

    os.utime(old, (1_000_000, 1_000_000))
    (data.backups_dir / "notes.txt").write_text("not a backup")
    assert [p.name for p in list_backups(data)] == ["new.zip", "old.zip"]


# -- reading and saving --------------------------------------------------------------------------


def test_an_untouched_window_sends_nothing_even_when_it_is_saved(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], monkeypatch: pytest.MonkeyPatch
) -> None:
    d = opened()
    assert d.build_patch() == {}
    monkeypatch.setattr(api_client, "put_settings", lambda patch: pytest.fail(f"sent {patch}"))
    d.save(close=True)
    assert d.result() == QDialog.DialogCode.Accepted


def test_every_page_exists_and_the_email_page_holds_the_mail_settings(
    opened: Callable[[], SettingsDialog],
) -> None:
    d = opened()
    assert d.nav.count() == len(PAGES) == 9
    assert [d.nav.item(i).text() for i in range(d.nav.count())] == PAGES
    assert {"keep_changed_versions", "disk_cap_gb", "backup_time", "debug_logging",
            "pause_on_battery_saver", "connectivity_url", "global_proxy",
            "notifications"} <= set(d.w)  # fmt: skip
    email = d.stack.widget(PAGES.index("Email")).widget()  # type: ignore[attr-defined]
    assert email.isAncestorOf(d.notifications.smtp_host)  # M6's settings, no longer a placeholder


def test_only_the_changed_fields_are_sent_and_the_engine_keeps_them(
    eng: EngineThread, api_client: ApiClient, opened: Callable[[], SettingsDialog],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    d = opened()
    saved: list[bool] = []
    d.saved.connect(lambda: saved.append(True))
    d.w["keep_changed_versions"].setValue(7)  # type: ignore[attr-defined]
    d.w["pause_on_battery_saver"].setChecked(True)  # type: ignore[attr-defined]
    d.w["timezone"].setText("Europe/Berlin")  # type: ignore[attr-defined]
    d.w["global_proxy"].setText("http://proxy.test:3128")  # type: ignore[attr-defined]
    d.w["backup_time"].setTime(d.w["backup_time"].time().fromString("04:15", "HH:mm"))  # type: ignore[attr-defined]
    d.sched_every.setValue(2)  # hours: the default schedule is hourly
    assert d.build_patch() == {
        "keep_changed_versions": 7,
        "pause_on_battery_saver": True,
        "timezone": "Europe/Berlin",
        "global_proxy": "http://proxy.test:3128",
        "backup_time": "04:15",
        "default_schedule": {"interval_s": 7200},
    }
    d.save(close=False)
    pump_until(lambda: saved, what="saved")
    now = api_client.settings()
    assert now.keep_changed_versions == 7 and now.pause_on_battery_saver is True
    assert now.timezone == "Europe/Berlin" and now.backup_time == "04:15"
    assert now.default_schedule.interval_s == 7200 and now.default_schedule.jitter_pct == 10
    assert d.note.text() == "Saved. The changes apply now." and d.isVisible() is False
    assert d.build_patch() == {}  # what was saved is the new starting point

    d.w["keep_changed_versions"].setValue(5)  # type: ignore[attr-defined]
    assert d.build_patch() == {"keep_changed_versions": 5}  # and a second change sends just it


def test_a_blank_nullable_field_clears_the_setting(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    api_client.put_settings({"global_proxy": "http://proxy.test:3128"})
    d = opened()
    assert d.w["global_proxy"].text() == "http://proxy.test:3128"  # type: ignore[attr-defined]
    d.w["global_proxy"].setText("   ")  # type: ignore[attr-defined]
    assert d.build_patch() == {"global_proxy": None}
    d.save(close=False)
    pump_until(lambda: api_client.settings().global_proxy is None, what="cleared")


def test_settings_read_at_start_up_are_labelled_and_saving_one_says_to_restart(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    d = opened()
    labels = {w.text() for w in d.findChildren(type(d.error)) if "needs restart" in w.text()}
    assert any(t.startswith("Processing processes") for t in labels)
    assert any(t.startswith("Wait after start") for t in labels)
    assert not any(t.startswith("Pages fetched at once") for t in labels)  # applies at once
    d.w["worker_processes"].setValue(3)  # type: ignore[attr-defined]
    d.save(close=False)
    pump_until(lambda: d.note.text().startswith("Saved."), what="saved")
    assert "Restart the engine for worker_processes" in d.note.text()
    assert api_client.settings().worker_processes == 3


def test_the_engines_refusal_is_shown_in_place_naming_the_field(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    d = opened()
    d.w["timezone"].setText("Not/AZone")  # type: ignore[attr-defined]
    d.save(close=True)
    pump_until(lambda: d.error.text() != "", what="the error")
    assert d.error.text().startswith("timezone:") and "Not/AZone" in d.error.text()
    assert d.result() != QDialog.DialogCode.Accepted  # still open, nothing lost
    assert api_client.settings().timezone is None
    d.w["timezone"].setText("")  # fixed: the same window can now save something else
    d.w["keep_changed_versions"].setValue(9)  # type: ignore[attr-defined]
    d.save(close=True)
    pump_until(lambda: d.result() == QDialog.DialogCode.Accepted, what="accepted")
    assert api_client.settings().keep_changed_versions == 9 and d.error.text() == ""


def test_debug_logging_is_switched_by_the_window_without_a_restart(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    d = opened()
    saved: list[bool] = []
    d.saved.connect(lambda: saved.append(True))
    try:
        d.w["debug_logging"].setChecked(True)  # type: ignore[attr-defined]
        d.save(close=False)
        pump_until(lambda: len(saved) == 1, what="first save")
        assert logging.getLogger().level == logging.DEBUG
        d.w["debug_logging"].setChecked(False)  # type: ignore[attr-defined]
        d.save(close=False)
        pump_until(lambda: len(saved) == 2, what="second save")
        assert logging.getLogger().level == logging.INFO
    finally:
        set_debug(False)


def test_an_edit_made_while_a_save_is_on_its_way_is_not_lost(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    d = opened()
    saved: list[bool] = []
    d.saved.connect(lambda: saved.append(True))
    d.w["keep_changed_versions"].setValue(7)  # type: ignore[attr-defined]
    d.save(close=False)
    assert not d.buttons.button(d.buttons.StandardButton.Apply).isEnabled()  # in flight
    d.save(close=False)  # a second click while it is on its way is ignored
    d.w["keep_changed_versions"].setValue(8)  # ... and so is not mistaken for what was sent
    pump_until(lambda: saved, what="first save")
    assert d.buttons.button(d.buttons.StandardButton.Apply).isEnabled()
    assert api_client.settings().keep_changed_versions == 7
    assert d.build_patch() == {"keep_changed_versions": 8}  # still an unsaved change
    d.save(close=False)
    pump_until(lambda: len(saved) == 2, what="second save")
    assert api_client.settings().keep_changed_versions == 8


# -- website exceptions --------------------------------------------------------------------------


def test_a_website_exception_can_be_added_and_then_removed(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    d = opened()
    d.add_override("Example.COM", 1, 5.0)
    assert d.build_patch() == {
        "host_overrides": {"example.com": {"concurrency": 1, "min_gap_s": 5.0}}
    }
    d.add_override()  # an untouched blank row is ignored
    d.save(close=False)
    pump_until(lambda: "example.com" in api_client.settings().host_overrides, what="added")
    ov = api_client.settings().host_overrides["example.com"]
    assert (ov.concurrency, ov.min_gap_s) == (1, 5.0)

    again = opened()  # a window opened later shows it
    assert again.overrides.item(0, 0).text() == "example.com" and again.build_patch() == {}
    again.overrides.selectRow(0)
    again.remove_selected_overrides()
    assert again.build_patch() == {"host_overrides": {"example.com": None}}
    again.save(close=False)
    pump_until(lambda: not api_client.settings().host_overrides, what="removed")


def test_an_empty_cell_means_use_the_default_and_a_bad_cell_is_caught_before_sending(
    api_client: ApiClient, opened: Callable[[], SettingsDialog], monkeypatch: pytest.MonkeyPatch
) -> None:
    d = opened()
    d.add_override("slow.test", None, 12.5)
    assert d.build_patch()["host_overrides"] == {
        "slow.test": {"concurrency": None, "min_gap_s": 12.5}
    }
    d.overrides.item(0, 1).setText("two")
    monkeypatch.setattr(api_client, "put_settings", lambda patch: pytest.fail(f"sent {patch}"))
    d.save(close=True)
    assert "whole number" in d.error.text() and "slow.test" in d.error.text()
    d.overrides.item(0, 1).setText("")
    d.overrides.item(0, 0).setText("")
    d.save(close=True)
    assert "name is empty" in d.error.text()


# -- backup --------------------------------------------------------------------------------------


def test_back_up_now_writes_a_zip_and_lists_it(
    eng: EngineThread, opened: Callable[[], SettingsDialog], pump_until: Callable[..., None]
) -> None:
    d = opened()
    assert d.last_backup.text() == "Last automatic backup: never" and d.backups.count() == 0
    d.btn_backup.click()
    assert not d.btn_backup.isEnabled()  # one at a time
    pump_until(lambda: d.backup_msg.text().startswith("Saved backup-"), what="backup")
    assert d.btn_backup.isEnabled() and d.backups.count() == 1
    (zip_,) = list(eng.data_dir.backups_dir.glob("*.zip"))
    assert d.backups.item(0).text().startswith(zip_.name)
    assert zip_.name in d.backup_msg.text() and zip_.stat().st_size > 0


def test_the_last_automatic_backup_time_comes_from_the_engines_health(
    opened: Callable[[], SettingsDialog], api_client: ApiClient
) -> None:
    d = opened()
    health = api_client.health().model_copy(update={"last_backup_at": "2026-03-01T03:00:12Z"})
    d._show_last_backup(health)
    assert d.last_backup.text().startswith("Last automatic backup: 2026-03-0")  # local time


def test_restore_asks_first_and_does_nothing_when_declined(
    eng: EngineThread, api_client: ApiClient, opened: Callable[[], SettingsDialog],
    monkeypatch: pytest.MonkeyPatch, pump_until: Callable[..., None],
) -> None:  # fmt: skip
    api_client.backup()
    d = opened()
    assert d.backups.count() == 1 and not d.btn_restore.isEnabled()
    d.backups.setCurrentRow(0)
    assert d.btn_restore.isEnabled()
    asked: list[str] = []
    monkeypatch.setattr(d, "_confirm", lambda title, text: asked.append(text) or False)
    d.btn_restore.click()
    assert asked and "pre-restore" in asked[0] and d.result() != QDialog.DialogCode.Accepted
    wait_idle(1000)
    assert not (eng.data_dir.root / "restore").exists()  # nothing was staged


def test_a_backup_that_is_not_one_is_refused_in_the_window(
    eng: EngineThread, opened: Callable[[], SettingsDialog], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, pump_until: Callable[..., None],
) -> None:  # fmt: skip
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"this is not a zip file")
    d = opened()
    monkeypatch.setattr(d, "_confirm", lambda *_a: True)
    monkeypatch.setattr(d, "_choose_backup_file", lambda: str(junk))
    d.btn_restore_file.click()
    pump_until(lambda: d.backup_msg.text().startswith("Not restored"), what="refusal")
    assert d.btn_restore_file.isEnabled() and d.btn_backup.isEnabled()
    assert d.result() != QDialog.DialogCode.Accepted


def test_restoring_through_the_window_restarts_into_the_backup_and_the_window_follows(
    tmp_path: Path, qapp: Any, pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    first = EngineThread(root).start()
    client = ApiClient(first.base, first.engine.token)
    events = EventStream(client.ws_url, client.token)
    main = MainWindow(client, events, data_dir=first.data_dir)
    second: EngineThread | None = None
    try:
        events.start()
        main.show()
        pump_until(lambda: events.connected, what="first connection")

        def add(name: str) -> None:
            first.site.set(f"/{name}", article(f"{name} tea", title=name))
            client.create_bookmark({"url": first.site.url(f"/{name}"), "name": name,
                                    "schedule": {"interval_s": 3600}})  # fmt: skip

        add("kept")
        client.backup()
        add("added-after-the-backup")
        assert {b.name for b in client.bookmarks().items} == {"kept", "added-after-the-backup"}

        dialog = SettingsDialog(client, client.settings(), first.data_dir, main)
        staged: list[str] = []
        dialog.restore_staged.connect(staged.append)
        monkeypatch.setattr(dialog, "_confirm", lambda *_a: True)
        dialog.backups.setCurrentRow(0)
        dialog.btn_restore.click()
        pump_until(lambda: staged, what="restore staged")
        assert dialog.result() == QDialog.DialogCode.Accepted  # nothing in it is current any more
        assert "restarting" in dialog.backup_msg.text()

        first.stop()  # the engine exits with code 3 ...
        pump_until(lambda: not events.connected, what="disconnect")
        second = EngineThread(root).start()  # ... and is started again, which applies the restore
        pump_until(lambda: events.connected, timeout=30, what="reconnection")
        assert client.token == second.engine.token
        assert [b.name for b in client.bookmarks().items] == ["kept"]
        assert any(p.name.startswith("pre-restore") for p in second.data_dir.backups_dir.iterdir())
    finally:
        wait_idle(2000)
        main.close()
        if second is not None:
            second.stop()


# -- data folder and capacity --------------------------------------------------------------------


def test_the_data_folder_page_shows_the_path_measures_sizes_and_opens_the_folder(
    eng: EngineThread, opened: Callable[[], SettingsDialog], monkeypatch: pytest.MonkeyPatch,
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    d = opened()
    assert d.folder_path.text() == str(eng.data_dir.root)
    assert d.sizes.text() == "Sizes are measured when this page opens."
    d.nav.setCurrentRow(P_DATA)
    pump_until(lambda: d.sizes.text().startswith("Database:"), what="sizes")
    lines = d.sizes.text().splitlines()
    assert [ln.split(":")[0] for ln in lines] == [
        "Database",
        "Page store",
        "Backups",
        "Logs",
        "Page store limit",
    ]
    assert lines[-1].endswith("used)") and "10.0 GB" in lines[-1]
    opened_paths: list[Path] = []
    monkeypatch.setattr(d, "_open_folder", opened_paths.append)
    d.btn_open_folder.click()
    assert opened_paths == [eng.data_dir.root]


def make(eng: EngineThread, client: ApiClient, name: str, **kw: Any) -> int:
    path = "/" + name
    eng.site.set(path, article(f"{name} tea", title=name))
    out = client.create_bookmark({"url": eng.site.url(path), "name": name,
                                  "schedule": {"interval_s": 60, "jitter_pct": 0}, **kw})  # fmt: skip
    return out.id


def test_the_capacity_page_shows_demand_per_pool_and_flags_a_website_over_its_limit(
    eng: EngineThread, api_client: ApiClient, opened: Callable[[], SettingsDialog],
    pump_until: Callable[..., None],
) -> None:  # fmt: skip
    api_client.autowatch("paused")  # nothing runs; only the numbers matter
    api_client.put_settings({"per_host_min_gap_s": 30.0})  # a host allows 2 checks a minute
    for n in ("a", "b", "c"):
        make(eng, api_client, n)  # three bookmarks, one minute each, all on the same host
    make(eng, api_client, "js", check_method="browser")
    d = opened()
    page(d, "Capacity")
    pump_until(lambda: d.pools.rowCount() == 2 and d.hosts.rowCount() == 1, what="capacity")

    cell = lambda t, r, c: t.item(r, c).text()  # noqa: E731
    assert cell(d.pools, 0, 0) == "Static" and cell(d.pools, 0, 1) == "3"
    assert cell(d.pools, 0, 2) == f"{3 / 60:.2f}/s" and "not measured yet" in cell(d.pools, 0, 3)
    assert cell(d.pools, 1, 0) == "Browser" and cell(d.pools, 1, 1) == "1"
    assert cell(d.hosts, 0, 0) == "127.0.0.1" and cell(d.hosts, 0, 1) == "4"
    assert cell(d.hosts, 0, 2) == "4.0/min" and cell(d.hosts, 0, 3) == "2.0/min"
    assert cell(d.hosts, 0, 4) == "200% over limit"
    assert "1 website(s) are asked for more checks" in d.capacity_summary.text()
    assert "c62828" in d.capacity_summary.styleSheet()

    api_client.put_settings({"per_host_min_gap_s": 5.0})  # 12 a minute: comfortable
    d.refresh_capacity()
    pump_until(lambda: d.capacity_summary.text() == "Nothing is over its limit.", what="refreshed")
    assert cell(d.hosts, 0, 3) == "12.0/min" and d.capacity_summary.styleSheet() == ""


# -- from the main window ------------------------------------------------------------------------


def test_the_main_window_opens_settings_with_a_shortcut_and_passes_the_data_folder(
    eng: EngineThread, qapp: Any, pump_until: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ApiClient(eng.base, eng.engine.token)
    main = MainWindow(client, None, data_dir=eng.data_dir)
    try:
        shown: list[SettingsDialog] = []
        monkeypatch.setattr(main, "_exec", lambda dialog: shown.append(dialog) or False)  # type: ignore[arg-type]
        assert [k.toString() for k in main.act_settings.shortcuts()] == ["Ctrl+,"]
        main.act_settings.trigger()
        pump_until(lambda: shown, what="settings dialog")
        assert shown[0].folder_path.text() == str(eng.data_dir.root)
        assert shown[0].build_patch() == {}
    finally:
        wait_idle(2000)
        main.close()
        client.close()
